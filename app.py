"""
PTUK TalentBridge — Flask Backend
===================================
Stack : Flask · SQLite · google-genai (Gemini 2.5 Flash)
Run   : python app.py
"""

import os
import json
import sqlite3
import hashlib
import secrets
import logging
from datetime import datetime
from functools import wraps

from flask import (
    Flask, request, jsonify, session,
    redirect, url_for, render_template, g
)
from google import genai

# ──────────────────────────────────────────────
#  App bootstrap
# ──────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = secrets.token_hex(32)   # regenerates each restart (fine for prototype)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
#  Config
# ──────────────────────────────────────────────
DB_PATH        = os.path.join(os.path.dirname(__file__), "students.db")
GEMINI_API_KEY = "AQ.Ab8RN6K6CqgCGrQRQJIa26hUkHt9J_41NVqLemZa1nSqraGWgA"
GEMINI_MODEL   = "gemini-2.5-flash"

ADMIN_EMAIL    = "admin@ptuk.edu.ps"
ADMIN_PASSWORD = "PTUK_Admin_2026"


# ──────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────
def hash_password(pw: str) -> str:
    """SHA-256 with a static salt (prototype-grade). Use bcrypt in production."""
    salt = "PTUK_SALT_2026"
    return hashlib.sha256(f"{salt}{pw}".encode()).hexdigest()


def get_db() -> sqlite3.Connection:
    """Return a per-request DB connection stored on Flask's `g`."""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db:
        db.close()


# ──────────────────────────────────────────────
#  Database initialisation
# ──────────────────────────────────────────────
def init_db():
    """Create tables and seed the default admin on first run."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS admins (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                email    TEXT    NOT NULL UNIQUE,
                password TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS resumes (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                anonymous_id       TEXT    NOT NULL UNIQUE,
                student_name       TEXT    NOT NULL DEFAULT 'Anonymous',
                specialization     TEXT    NOT NULL DEFAULT 'General',
                graduation_year    TEXT    NOT NULL DEFAULT '',
                full_text          TEXT    NOT NULL,
                upload_timestamp   TEXT    NOT NULL
            );
        """)

        # Seed default admin (idempotent)
        existing = conn.execute(
            "SELECT id FROM admins WHERE email = ?", (ADMIN_EMAIL,)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO admins (email, password) VALUES (?, ?)",
                (ADMIN_EMAIL, hash_password(ADMIN_PASSWORD))
            )
            log.info("Default admin seeded → %s", ADMIN_EMAIL)

    log.info("Database ready at %s", DB_PATH)


# ──────────────────────────────────────────────
#  Auth decorator
# ──────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("index") + "#login-section")
        return f(*args, **kwargs)
    return decorated


# ──────────────────────────────────────────────
#  Routes — public
# ──────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


# ──────────────────────────────────────────────
#  Routes — auth
# ──────────────────────────────────────────────
@app.route("/login", methods=["POST"])
def login():
    data  = request.get_json(silent=True) or {}
    email = data.get("email", "").strip().lower()
    pw    = data.get("password", "")

    if not email or not pw:
        return jsonify({"success": False, "error": "Email and password are required."}), 400

    db   = get_db()
    admin = db.execute(
        "SELECT * FROM admins WHERE email = ? AND password = ?",
        (email, hash_password(pw))
    ).fetchone()

    if not admin:
        log.warning("Failed login attempt for: %s", email)
        return jsonify({"success": False, "error": "Invalid credentials."}), 401

    session["admin_logged_in"] = True
    session["admin_email"]     = email
    log.info("Admin logged in: %s", email)
    return jsonify({"success": True})


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"success": True})


# ──────────────────────────────────────────────
#  Routes — admin (protected)
# ──────────────────────────────────────────────
@app.route("/admin/dashboard")
@login_required
def admin_dashboard():
    """Return aggregate stats for the admin overview cards."""
    db = get_db()
    total_resumes = db.execute("SELECT COUNT(*) FROM resumes").fetchone()[0]
    fields_count  = db.execute(
        "SELECT COUNT(DISTINCT specialization) FROM resumes"
    ).fetchone()[0]
    return jsonify({
        "total_resumes": total_resumes,
        "fields_covered": fields_count,
    })


@app.route("/admin/resumes")
@login_required
def admin_resumes():
    """Return all CVs for the data table."""
    db   = get_db()
    rows = db.execute(
        """SELECT id, anonymous_id, student_name, specialization,
                  graduation_year, upload_timestamp
           FROM resumes ORDER BY id DESC"""
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/admin/upload", methods=["POST"])
@login_required
def admin_upload():
    """Add a new CV to the pool."""
    data  = request.get_json(silent=True) or {}
    name  = data.get("student_name", "").strip()
    field = data.get("specialization", "").strip()
    year  = data.get("graduation_year", "").strip()
    text  = data.get("full_text", "").strip()

    if not name or not field or len(text) < 100:
        return jsonify({"success": False,
                        "error": "Name, specialization, and CV text (≥100 chars) are required."}), 400

    db = get_db()
    # Generate a guaranteed-unique anonymous ID
    existing_ids = {r[0] for r in db.execute("SELECT anonymous_id FROM resumes").fetchall()}
    while True:
        anon_id = "PTUK-" + secrets.token_hex(3).upper()
        if anon_id not in existing_ids:
            break

    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    try:
        db.execute(
            """INSERT INTO resumes
               (anonymous_id, student_name, specialization, graduation_year, full_text, upload_timestamp)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (anon_id, name, field, year, text, timestamp)
        )
        db.commit()
    except sqlite3.IntegrityError as e:
        log.error("DB insert error: %s", e)
        return jsonify({"success": False, "error": "Database error."}), 500

    log.info("CV uploaded → %s (%s)", anon_id, name)
    return jsonify({"success": True, "anonymous_id": anon_id, "timestamp": timestamp})


@app.route("/admin/delete/<int:resume_id>", methods=["DELETE"])
@login_required
def admin_delete(resume_id):
    """Remove a CV from the pool."""
    db = get_db()
    row = db.execute("SELECT id FROM resumes WHERE id = ?", (resume_id,)).fetchone()
    if not row:
        return jsonify({"success": False, "error": "Resume not found."}), 404

    db.execute("DELETE FROM resumes WHERE id = ?", (resume_id,))
    db.commit()
    log.info("CV deleted → id=%s", resume_id)
    return jsonify({"success": True})


# ──────────────────────────────────────────────
#  Routes — AI matching engine
# ──────────────────────────────────────────────
@app.route("/match", methods=["POST"])
def match():
    """
    Fetch all resumes → build Gemini prompt → parse JSON response →
    return top-10 ranked candidates to the frontend.
    """
    data       = request.get_json(silent=True) or {}
    job_title  = data.get("job_title",  "").strip()
    job_field  = data.get("job_field",  "").strip()
    emp_type   = data.get("emp_type",   "").strip()
    skills     = data.get("skills",     "").strip()
    job_desc   = data.get("job_desc",   "").strip()

    if not all([job_title, job_field, skills, job_desc]):
        return jsonify({"success": False, "error": "All job fields are required."}), 400

    # Pull all resumes from DB
    db      = get_db()
    resumes = db.execute(
        "SELECT id, anonymous_id, specialization, graduation_year, full_text FROM resumes"
    ).fetchall()

    if not resumes:
        return jsonify({"success": False,
                        "error": "No resumes in the pool yet. Ask the admin to upload CVs first."}), 422

    # Build the resume block for the prompt
    resume_block = "\n\n".join([
        f"--- CANDIDATE {r['anonymous_id']} ---\n"
        f"Specialization : {r['specialization']}\n"
        f"Graduation Year: {r['graduation_year']}\n"
        f"CV Content     :\n{r['full_text']}"
        for r in resumes
    ])

    prompt = f"""
You are an expert AI recruitment engine for Palestine Technical University – Kadoorie (PTUK).
Your task is to evaluate and rank student CVs against a recruiter's job opening.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
JOB OPENING DETAILS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Job Title        : {job_title}
Field / Sector   : {job_field}
Employment Type  : {emp_type}
Required Skills  : {skills}
Job Description  :
{job_desc}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CANDIDATE POOL ({len(resumes)} candidates)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{resume_block}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INSTRUCTIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Evaluate EVERY candidate against the job requirements.
Consider: skill overlap, educational field alignment, experience relevance, graduation year, projects, and overall suitability.

Return a JSON array of ALL candidates, sorted from HIGHEST match score to LOWEST.
Include a maximum of 10 entries (or fewer if less than 10 candidates exist).

Each object in the array MUST have exactly these keys:
  "rank"           : integer starting at 1 (1 = best match)
  "student_id"     : the candidate's anonymous_id string exactly as provided
  "matching_score" : integer from 0 to 100 (percentage match)
  "reason"         : a single, concise, punchy professional sentence written ENTIRELY IN ARABIC
                     explaining why this candidate is a strong match for this specific role.

Rules:
- Scores must be realistic and differentiated (do not give everyone 90+).
- The "reason" field MUST be in Arabic only. No English in that field.
- Output ONLY the raw JSON array. No markdown, no explanation, no preamble.
""".strip()

    # Call Gemini 2.5 Flash
    try:
        client   = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=genai.types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.3,
                max_output_tokens=4096,
            ),
        )
        raw_text = response.text.strip()
        log.info("Gemini raw response (first 300 chars): %s", raw_text[:300])
    except Exception as e:
        log.error("Gemini API error: %s", e)
        return jsonify({"success": False, "error": f"AI engine error: {str(e)}"}), 502

    # Parse JSON
    try:
        # Strip markdown fences if model added them despite the mime type
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        candidates = json.loads(raw_text)
        if not isinstance(candidates, list):
            raise ValueError("Expected a JSON array")
    except (json.JSONDecodeError, ValueError) as e:
        log.error("JSON parse error: %s\nRaw: %s", e, raw_text)
        return jsonify({"success": False,
                        "error": "AI returned an unexpected format. Please try again."}), 502

    # Validate & sanitise each entry
    clean = []
    for item in candidates[:10]:
        try:
            clean.append({
                "rank":           int(item.get("rank", 0)),
                "student_id":     str(item.get("student_id", "")),
                "matching_score": max(0, min(100, int(item.get("matching_score", 0)))),
                "reason":         str(item.get("reason", "")),
            })
        except (TypeError, ValueError):
            continue  # skip malformed entries

    log.info("Matching complete: %d results returned for '%s'", len(clean), job_title)
    return jsonify({
        "success":    True,
        "results":    clean,
        "pool_size":  len(resumes),
        "job_title":  job_title,
    })


# ──────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)
