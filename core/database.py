"""
core/database.py — DB helper.

Now uses MySQL via DATABASE_URL in .env.
PyMySQL is used as the driver (pure Python, no C deps).
"""

import os
import re
import pymysql
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from datetime import datetime, UTC
from flask_sqlalchemy import SQLAlchemy

# Required for SQLAlchemy to use PyMySQL as the MySQL driver
pymysql.install_as_MySQLdb()

# ── Single db instance — import this everywhere ───────────────────────────────
db = SQLAlchemy()

DATABASE_URL = os.environ.get('DATABASE_URL')

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set. Add it to your .env file.")

engine       = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Database:
    """High-level helper used by monitor.py and the Gmail dashboard."""

    def __init__(self):
        from core.models import JobApplication
        JobApplication.metadata.create_all(bind=engine)

    def _session(self):
        return SessionLocal()

    def get_all_applications(self):
        from core.models import JobApplication
        session = self._session()
        try:
            return session.query(JobApplication).order_by(
                JobApplication.last_activity.desc()
            ).all()
        finally:
            session.close()

    def get_all_message_ids(self) -> list[str]:
        from core.models import JobApplication
        session = self._session()
        try:
            rows = session.query(JobApplication.message_id).all()
            return [r[0] for r in rows if r[0]]
        finally:
            session.close()

    def save_application(self, email: dict, analysis: dict):
        from core.models import JobApplication
        session = self._session()
        try:
            existing = session.query(JobApplication).filter_by(
                message_id=email.get('id')
            ).first()
            if existing:
                return

            record = JobApplication(
                message_id       = email.get('id'),
                thread_id        = email.get('threadId'),
                subject          = email.get('subject', ''),
                email            = email.get('sender', ''),
                body             = email.get('body', ''),
                company          = _extract_company(email.get('sender', '')),
                is_recruiter     = analysis.get('is_recruiter', False),
                ai_summary       = analysis.get('summary', ''),
                confidence_score = analysis.get('confidence_score', 0),
                status           = 'New',
                last_activity    = datetime.now(UTC),
            )
            session.add(record)
            session.commit()
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()


def _extract_company(sender: str) -> str:
    match = re.search(r'@([\w.-]+)', sender)
    if match:
        parts = match.group(1).split('.')
        return parts[-2].capitalize() if len(parts) >= 2 else match.group(1)
    return sender.split('<')[0].strip() or 'Unknown'