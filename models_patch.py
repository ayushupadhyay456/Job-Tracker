"""
models_patch.py
───────────────
Reference for what the final User model should look like in core/models.py.
All columns listed here are already applied in the models.py output file.
The migration script (migration_add_user_prefs.py) adds the new columns
to an existing database without touching existing data.
"""

# Columns already present in original models.py (DO NOT duplicate):
#   id, email, name, password_hash, picture, google_id, resume_text,
#   created_at, plan, subscription_id, subscription_status, subscription_ends,
#   domain, resume_path, inferred_role, experience_level, onboarding_complete,
#   ats_improved_text, ats_original_score, ats_improved_score, ats_improved_at

# ── NEW columns added in this update ─────────────────────────────────────────
# Copy these into class User(db.Model) in core/models.py if not already there:

#   preferred_location  = db.Column(db.String(255), nullable=True)
#   work_type           = db.Column(db.String(255), nullable=True)
#   employment_type     = db.Column(db.String(255), nullable=True)
#   salary_min          = db.Column(db.Integer,     nullable=True)
#   salary_max          = db.Column(db.Integer,     nullable=True)
#   skills              = db.Column(db.Text,        nullable=True)
#   saved_job_ids       = db.Column(db.JSON,        nullable=True)

# ── To apply to an existing database ─────────────────────────────────────────
#   python migration_add_user_prefs.py