"""
PTUK TalentBridge — Flask Backend
===================================
Stack  : Flask · SQLite · google-genai (Gemini 2.5 Flash)
Deploy : Render / Gunicorn
"""

import os
import json
import sqlite3
import hashlib
import secrets
import logging
from datetime import datetime
from functools import wraps

from flask import Flask, request, jsonify, session, render_template, g
from google import genai

# ──────────────────────────────────────────────
#  App bootstrap
# ──────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "PTUK_FALLBACK_SECRET_2026")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
#  Config — all from environment variables
# ──────────────────────────────────────────────
DB_PATH        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "students.db")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AQ.Ab8RN6K6CqgCGrQRQJIa26hUkHt9J_41NVqLemZa1nSqraGWgA")
GEMINI_MODEL   = "gemini-2.5-flash"
ADMIN_EMAIL    = os.environ.get("ADMIN_EMAIL",    "admin@ptuk.edu.ps")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "PTUK_Admin_2026")


# ──────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────
def hash_password(pw: str) -> str:
    salt = "PTUK_SALT_2026"
    return hashlib.sha256(f"{salt}{pw}".encode()).hexdigest()


def get_db() -> sqlite3.Connection:
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
#  Called at module level so gunicorn triggers it
# ──────────────────────────────────────────────
def init_db():
    try:
        log.info("Initialising database at: %s", DB_PATH)
        with sqlite3.connect(DB_PATH) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS admins (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    email    TEXT    NOT NULL UNIQUE,
                    password TEXT    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS resumes (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    anonymous_id     TEXT    NOT NULL UNIQUE,
                    student_name     TEXT    NOT NULL DEFAULT 'Anonymous',
                    specialization   TEXT    NOT NULL DEFAULT 'General',
                    graduation_year  TEXT    NOT NULL DEFAULT '',
                    full_text        TEXT    NOT NULL,
                    upload_timestamp TEXT    NOT NULL
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
                conn.commit()
                log.info("Default admin seeded → %s", ADMIN_EMAIL)
            else:
                log.info("Admin already exists → %s", ADMIN_EMAIL)

        log.info("Database initialisation complete.")
    except Exception as e:
        log.error("CRITICAL: Database init failed: %s", e)


# ── Run immediately on import (gunicorn + flask dev both trigger this) ──
init_db()


# ──────────────────────────────────────────────
#  Auth decorator
# ──────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return jsonify({"success": False, "error": "Unauthorized. Please log in."}), 401
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
    try:
        data  = request.get_json(silent=True) or {}
        email = data.get("email", "").strip().lower()
        pw    = data.get("password", "")

        if not email or not pw:
            return jsonify({"success": False, "error": "Email and password are required."}), 400

        # ── FAIL-SAFE: hardcoded env-var check (bypasses SQLite) ──
        env_email = (ADMIN_EMAIL or "").strip().lower()
        env_pass  = (ADMIN_PASSWORD or "").strip()
        if email == env_email and pw == env_pass:
            session["admin_logged_in"] = True
            session["admin_email"]     = email
            log.info("Admin logged in via env-var fallback: %s", email)
            return jsonify({"success": True})

        # ── Normal DB check ──
        try:
            db    = get_db()
            admin = db.execute(
                "SELECT * FROM admins WHERE email = ? AND password = ?",
                (email, hash_password(pw))
            ).fetchone()
            if admin:
                session["admin_logged_in"] = True
                session["admin_email"]     = email
                log.info("Admin logged in via DB: %s", email)
                return jsonify({"success": True})
        except Exception as db_err:
            log.warning("DB login check failed (%s) — env fallback already handled.", db_err)

        log.warning("Failed login attempt for: %s", email)
        return jsonify({"success": False, "error": "Invalid credentials."}), 401

    except Exception as e:
        log.error("Login route error: %s", e)
        return jsonify({"success": False, "error": "Server error during login."}), 500


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
    try:
        db            = get_db()
        total_resumes = db.execute("SELECT COUNT(*) FROM resumes").fetchone()[0]
        fields_count  = db.execute(
            "SELECT COUNT(DISTINCT specialization) FROM resumes"
        ).fetchone()[0]
        return jsonify({"total_resumes": total_resumes, "fields_covered": fields_count})
    except Exception as e:
        log.error("Dashboard error: %s", e)
        return jsonify({"total_resumes": 0, "fields_covered": 0})


@app.route("/admin/resumes")
@login_required
def admin_resumes():
    try:
        db   = get_db()
        rows = db.execute(
            """SELECT id, anonymous_id, student_name, specialization,
                      graduation_year, upload_timestamp
               FROM resumes ORDER BY id DESC"""
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        log.error("Resumes fetch error: %s", e)
        return jsonify([])


@app.route("/admin/upload", methods=["POST"])
@login_required
def admin_upload():
    try:
        data  = request.get_json(silent=True) or {}
        name  = data.get("student_name",  "").strip()
        field = data.get("specialization","").strip()
        year  = data.get("graduation_year","").strip()
        text  = data.get("full_text",     "").strip()

        if not name or not field or len(text) < 100:
            return jsonify({
                "success": False,
                "error": "Name, specialization, and CV text (≥100 chars) are required."
            }), 400

        db = get_db()
        existing_ids = {r[0] for r in db.execute("SELECT anonymous_id FROM resumes").fetchall()}
        while True:
            anon_id = "PTUK-" + secrets.token_hex(3).upper()
            if anon_id not in existing_ids:
                break

        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        db.execute(
            """INSERT INTO resumes
               (anonymous_id, student_name, specialization, graduation_year, full_text, upload_timestamp)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (anon_id, name, field, year, text, timestamp)
        )
        db.commit()
        log.info("CV uploaded → %s (%s)", anon_id, name)
        return jsonify({"success": True, "anonymous_id": anon_id, "timestamp": timestamp})

    except sqlite3.IntegrityError as e:
        log.error("DB integrity error on upload: %s", e)
        return jsonify({"success": False, "error": "Duplicate entry — please try again."}), 409
    except Exception as e:
        log.error("Upload error: %s", e)
        return jsonify({"success": False, "error": f"Server error: {str(e)}"}), 500


@app.route("/admin/delete/<int:resume_id>", methods=["DELETE"])
@login_required
def admin_delete(resume_id):
    try:
        db  = get_db()
        row = db.execute("SELECT id FROM resumes WHERE id = ?", (resume_id,)).fetchone()
        if not row:
            return jsonify({"success": False, "error": "Resume not found."}), 404
        db.execute("DELETE FROM resumes WHERE id = ?", (resume_id,))
        db.commit()
        log.info("CV deleted → id=%s", resume_id)
        return jsonify({"success": True})
    except Exception as e:
        log.error("Delete error: %s", e)
        return jsonify({"success": False, "error": f"Server error: {str(e)}"}), 500


# ──────────────────────────────────────────────
#  Routes — AI matching engine
# ──────────────────────────────────────────────
@app.route("/match", methods=["POST"])
def match():
    try:
        data      = request.get_json(silent=True) or {}
        job_title = data.get("job_title", "").strip()
        job_field = data.get("job_field", "").strip()
        emp_type  = data.get("emp_type",  "").strip()
        skills    = data.get("skills",    "").strip()
        job_desc  = data.get("job_desc",  "").strip()

        if not all([job_title, job_field, skills, job_desc]):
            return jsonify({"success": False, "error": "All job fields are required."}), 400

        try:
            db      = get_db()
            resumes = db.execute(
                "SELECT id, anonymous_id, specialization, graduation_year, full_text FROM resumes"
            ).fetchall()
        except Exception as db_err:
            log.error("DB fetch error during matching: %s", db_err)
            return jsonify({"success": False, "error": "Could not read CV pool from database."}), 500

        if not resumes:
            return jsonify({
                "success": False,
                "error": "No resumes in the pool yet. Ask the admin to upload CVs first."
            }), 422

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
Consider: skill overlap, field alignment, experience, graduation year, projects, suitability.

Return a JSON array sorted HIGHEST to LOWEST match score. Maximum 10 entries.

Each object MUST have exactly:
  "rank"           : integer starting at 1
  "student_id"     : the anonymous_id string exactly as given
  "matching_score" : integer 0–100
  "reason"         : one concise professional sentence in ARABIC ONLY

Rules:
- Scores must be realistic and differentiated.
- "reason" must be Arabic only — no English.
- Output ONLY the raw JSON array. No markdown, no preamble.
""".strip()

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
            log.info("Gemini response (first 300 chars): %s", raw_text[:300])
        except Exception as ai_err:
            log.error("Gemini API error: %s", ai_err)
            return jsonify({"success": False, "error": f"AI engine error: {str(ai_err)}"}), 502

        try:
            if raw_text.startswith("```"):
                raw_text = raw_text.split("```")[1]
                if raw_text.startswith("json"):
                    raw_text = raw_text[4:]
            candidates = json.loads(raw_text)
            if not isinstance(candidates, list):
                raise ValueError("Expected a JSON array")
        except (json.JSONDecodeError, ValueError) as parse_err:
            log.error("JSON parse error: %s\nRaw: %s", parse_err, raw_text)
            return jsonify({
                "success": False,
                "error": "AI returned an unexpected format. Please try again."
            }), 502

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
                continue

        log.info("Matching complete: %d results for '%s'", len(clean), job_title)
        return jsonify({
            "success":   True,
            "results":   clean,
            "pool_size": len(resumes),
            "job_title": job_title,
        })

    except Exception as e:
        log.error("Unhandled match error: %s", e)
        return jsonify({"success": False, "error": f"Unexpected server error: {str(e)}"}), 500


# ──────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
