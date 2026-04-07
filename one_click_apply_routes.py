# ─────────────────────────────────────────────────────────────────────────────
# ONE-CLICK APPLY ROUTES
# ─────────────────────────────────────────────────────────────────────────────
# Add these imports at the top of app.py (next to existing imports):
#
#   from core.models import db, User, OneClickApplication
#   import os, re
#   (requests is already imported via jobs_service, but add it here if needed)
#
# Then paste the four routes below anywhere before  if __name__ == '__main__'
# ─────────────────────────────────────────────────────────────────────────────

import os, re, requests as _requests   # _requests alias avoids collision if already imported


# ── Helper: AI cover-letter generator ────────────────────────────────────────

def _generate_cover_letter(user, job_title: str, company: str, job_description: str) -> str:
    """
    Uses Gemini to produce a short, ATS-friendly cover letter.
    Falls back to a clean template if Gemini is unavailable.
    """
    resume_snippet = (user.resume_text or "")[:2000]
    name           = user.name or "Applicant"

    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    if GEMINI_API_KEY:
        try:
            from google import genai
            from google.genai import types as gtypes

            client = genai.Client(api_key=GEMINI_API_KEY)
            prompt = f"""
You are an expert career coach. Write a concise, professional cover letter (3 short paragraphs, ~150 words).
Do NOT use generic filler phrases like "I am writing to express my interest".
Tailor it tightly to the job. Output ONLY the letter body (no subject, no "Dear Hiring Manager" header).

Candidate name: {name}
Target role: {job_title} at {company}
Job description (excerpt): {job_description[:800]}
Resume excerpt: {resume_snippet}
"""
            resp = client.models.generate_content(
                model    = "gemini-1.5-flash",
                contents = prompt,
                config   = gtypes.GenerateContentConfig(
                    max_output_tokens = 400,
                    temperature       = 0.4,
                ),
            )
            return resp.text.strip()
        except Exception as e:
            print(f"⚠️  Cover letter generation failed: {e}")

    # ── Fallback template ────────────────────────────────────────────────────
    skills_preview = ""
    if resume_snippet:
        # Extract first ~60 chars after "skills" keyword as a hint
        m = re.search(r'skills?\W+(.{30,80})', resume_snippet, re.IGNORECASE)
        if m:
            skills_preview = m.group(1).split('\n')[0].strip()

    return (
        f"I am excited to apply for the {job_title} position at {company}. "
        f"My background aligns well with the requirements of this role"
        + (f", particularly my experience with {skills_preview}" if skills_preview else "")
        + ".\n\n"
        "Throughout my career I have consistently delivered results by combining "
        "technical rigour with strong collaboration skills. I thrive in fast-paced "
        "environments and enjoy solving complex problems with clean, maintainable solutions.\n\n"
        f"I would love the opportunity to bring this experience to {company} and "
        "contribute to your team's goals. Thank you for your consideration."
    )


# ── Route 1: Preview cover letter (called when modal opens) ──────────────────

@app.route('/jobs/apply/preview', methods=['POST'])
@login_required
def jobs_apply_preview():
    """
    Returns an AI-generated cover letter for the user to review before submitting.
    Does NOT write to the DB yet.
    """
    guard = _require_active_subscription()
    if guard:
        return jsonify({"ok": False, "error": "Subscription required"}), 403

    data        = request.get_json() or {}
    job_title   = (data.get('job_title')   or '').strip()
    company     = (data.get('company')     or '').strip()
    description = (data.get('description') or '').strip()

    if not job_title or not company:
        return jsonify({"ok": False, "error": "job_title and company are required"}), 400

    cover_letter = _generate_cover_letter(
        current_user, job_title, company, description
    )
    return jsonify({"ok": True, "cover_letter": cover_letter})


# ── Route 2: Confirm & record the application ─────────────────────────────────

@app.route('/jobs/apply/confirm', methods=['POST'])
@login_required
def jobs_apply_confirm():
    """
    Saves the OneClickApplication record to the DB.
    The user may have edited the cover letter in the modal before confirming.
    """
    guard = _require_active_subscription()
    if guard:
        return jsonify({"ok": False, "error": "Subscription required"}), 403

    data        = request.get_json() or {}
    job_title   = (data.get('job_title')   or '').strip()
    company     = (data.get('company')     or '').strip()
    job_id      = (data.get('job_id')      or '').strip() or None
    job_url     = (data.get('job_url')     or '').strip() or None
    location    = (data.get('location')    or '').strip() or None
    source      = (data.get('source')      or '').strip() or None
    match_score = data.get('match_score')
    cover_letter = (data.get('cover_letter') or '').strip() or None

    if not job_title or not company:
        return jsonify({"ok": False, "error": "job_title and company are required"}), 400

    # Prevent duplicate applications for the same job
    existing = OneClickApplication.query.filter_by(
        user_id   = current_user.id,
        job_title = job_title,
        company   = company,
    ).first()
    if existing:
        return jsonify({
            "ok":      True,
            "already_applied": True,
            "applied_at": existing.applied_at.isoformat(),
        })

    try:
        ms = int(match_score) if match_score is not None else None
    except (ValueError, TypeError):
        ms = None

    application = OneClickApplication(
        user_id      = current_user.id,
        job_id       = job_id,
        job_title    = job_title,
        company      = company,
        location     = location,
        job_url      = job_url,
        source       = source,
        match_score  = ms,
        cover_letter = cover_letter,
        status       = "Applied",
    )
    db.session.add(application)
    db.session.commit()

    return jsonify({
        "ok":         True,
        "id":         application.id,
        "applied_at": application.applied_at.isoformat(),
    })


# ── Route 3: List all applications for current user ──────────────────────────

@app.route('/jobs/applications', methods=['GET'])
@login_required
def jobs_applications():
    """
    Returns the full application history for the logged-in user as JSON.
    Used by the "My Applications" panel on the jobs page.
    """
    guard = _require_active_subscription()
    if guard:
        return jsonify({"ok": False, "error": "Subscription required"}), 403

    apps = (
        OneClickApplication.query
        .filter_by(user_id=current_user.id)
        .order_by(OneClickApplication.applied_at.desc())
        .all()
    )
    return jsonify({"ok": True, "applications": [a.to_dict() for a in apps]})


# ── Route 4: Update application status ───────────────────────────────────────

@app.route('/jobs/applications/<int:app_id>/status', methods=['PATCH'])
@login_required
def jobs_application_status(app_id):
    """
    Lets the user update the status of an application from the tracker panel.
    Valid statuses: Applied | Interviewing | Offered | Rejected | Withdrawn
    """
    guard = _require_active_subscription()
    if guard:
        return jsonify({"ok": False, "error": "Subscription required"}), 403

    VALID_STATUSES = {"Applied", "Interviewing", "Offered", "Rejected", "Withdrawn"}

    application = OneClickApplication.query.filter_by(
        id=app_id, user_id=current_user.id
    ).first_or_404()

    data       = request.get_json() or {}
    new_status = (data.get('status') or '').strip()

    if new_status not in VALID_STATUSES:
        return jsonify({"ok": False, "error": f"Invalid status. Choose from: {', '.join(VALID_STATUSES)}"}), 400

    application.status = new_status
    db.session.commit()
    return jsonify({"ok": True, "status": new_status})
