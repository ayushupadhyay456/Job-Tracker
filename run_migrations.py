"""
run_migrations.py
─────────────────
Applies all pending schema changes directly via SQLAlchemy.
Run this ONCE inside your container:

    docker exec -it job-mail-scheduler python run_migrations.py

Safe to run multiple times — each ALTER is wrapped in a column-existence check.
"""

import os
import pymysql
pymysql.install_as_MySQLdb()

from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL env var is not set")

engine = create_engine(DATABASE_URL)


def column_exists(conn, table: str, column: str) -> bool:
    result = conn.execute(text(
        "SELECT COUNT(*) FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME = :table AND COLUMN_NAME = :col"
    ), {"table": table, "col": column})
    return result.scalar() > 0


def table_exists(conn, table: str) -> bool:
    result = conn.execute(text(
        "SELECT COUNT(*) FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table"
    ), {"table": table})
    return result.scalar() > 0


with engine.begin() as conn:

    # ── 1. ATS improvement columns on `users` ────────────────────────────────
    ats_cols = [
        ("ats_improved_text",   "LONGTEXT"),
        ("ats_original_score",  "INT"),
        ("ats_improved_score",  "INT"),
        ("ats_improved_at",     "DATETIME"),
    ]
    for col_name, col_type in ats_cols:
        if not column_exists(conn, "users", col_name):
            conn.execute(text(f"ALTER TABLE users ADD COLUMN {col_name} {col_type} NULL"))
            print(f"  ✅ Added users.{col_name} ({col_type})")
        else:
            print(f"  ⏭  users.{col_name} already exists — skipped")

    # ── 2. one_click_applications table ─────────────────────────────────────
    if not table_exists(conn, "one_click_applications"):
        conn.execute(text("""
            CREATE TABLE one_click_applications (
                id           INT AUTO_INCREMENT PRIMARY KEY,
                user_id      INT          NOT NULL,
                job_id       VARCHAR(255) NULL,
                job_title    VARCHAR(255) NOT NULL,
                company      VARCHAR(255) NOT NULL,
                location     VARCHAR(255) NULL,
                job_url      VARCHAR(512) NULL,
                source       VARCHAR(100) NULL,
                match_score  INT          NULL,
                cover_letter LONGTEXT     NULL,
                status       VARCHAR(50)  NOT NULL DEFAULT 'Applied',
                applied_at   DATETIME     NULL,
                updated_at   DATETIME     NULL,
                CONSTRAINT fk_oca_user FOREIGN KEY (user_id)
                    REFERENCES users(id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """))
        conn.execute(text(
            "CREATE INDEX ix_one_click_applications_user_id "
            "ON one_click_applications (user_id)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_oca_user_title_company "
            "ON one_click_applications (user_id, job_title(100), company(100))"
        ))
        print("  ✅ Created table one_click_applications + indexes")
    else:
        print("  ⏭  one_click_applications already exists — skipped")

print("\n✅ All migrations complete.")
