from datetime import datetime, UTC
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from .database import db


class User(db.Model, UserMixin):
    __tablename__ = "users"

    id            = db.Column(db.Integer, primary_key=True)
    email         = db.Column(db.String(255), unique=True, nullable=False)
    name          = db.Column(db.String(255))
    password_hash = db.Column(db.String(255))
    picture       = db.Column(db.String(512))
    google_id     = db.Column(db.String(255), unique=True)
    resume_text   = db.Column(db.Text)
    created_at    = db.Column(db.DateTime, default=lambda: datetime.now(UTC))

    plan                = db.Column(db.String(50),  default="monthly")
    subscription_id     = db.Column(db.String(200), nullable=True)
    subscription_status = db.Column(db.String(50),  default="inactive")
    subscription_ends   = db.Column(db.DateTime,    nullable=True)
    domain              = db.Column(db.String(100), default="Software Engineer")
    resume_path         = db.Column(db.String(255), nullable=True)
    inferred_role       = db.Column(db.String(100), default="Software Engineer")
    experience_level    = db.Column(db.String(50),  nullable=True)  # Entry / Mid / Senior

    # ── NEW: tracks whether user has completed post-payment onboarding ───────
    onboarding_complete = db.Column(db.Boolean, default=False)

    # Relationship
    remote_applications = db.relationship("RemoteApplication", backref="user", lazy="dynamic")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def monthly_score_limit(self):
        PLAN_LIMITS = {"monthly": 10, "biannual": 70, "annual": 150}
        if not self.has_access:
            return 0
        return PLAN_LIMITS.get(self.plan, 0)

    @property
    def has_access(self):
        if self.subscription_status == "active":
            return True
        if self.subscription_status == "cancelled" and self.subscription_ends:
            return self.subscription_ends > datetime.now(UTC)
        return False

    def can_score(self, scores_used):
        limit = self.monthly_score_limit
        if limit == -1:
            return True
        return scores_used < limit

    def scores_remaining(self, scores_used):
        limit = self.monthly_score_limit
        if limit == -1:
            return 9999
        return max(0, limit - scores_used)

    def __repr__(self):
        return f"<User {self.email} plan={self.plan}>"


class RemoteApplication(db.Model):
    __tablename__ = "remote_applications"
    id         = db.Column(db.Integer, primary_key=True)
    company    = db.Column(db.String(255))
    job_title  = db.Column(db.String(255))
    status     = db.Column(db.String(50), default="Applied")
    applied_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC))
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)


class JobApplication(db.Model):
    __tablename__ = "job_applications"
    id               = db.Column(db.Integer, primary_key=True)
    message_id       = db.Column(db.String(255), unique=True)
    thread_id        = db.Column(db.String(255))
    subject          = db.Column(db.String(512))
    email            = db.Column(db.String(255))
    body             = db.Column(db.Text)
    company          = db.Column(db.String(255))
    is_recruiter     = db.Column(db.Boolean, default=False)
    ai_summary       = db.Column(db.Text)
    confidence_score = db.Column(db.Integer)
    status           = db.Column(db.String(50))
    last_activity    = db.Column(db.DateTime)