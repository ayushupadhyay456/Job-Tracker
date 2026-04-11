"""
migration_add_user_prefs.py
───────────────────────────
Adds missing preference/onboarding columns to the `users` table.
Safe to run multiple times — existing columns are silently skipped.

Usage:  python migration_add_user_prefs.py
"""
import os, sys

def run():
    try:
        from flask import current_app
        _app = current_app._get_current_object()
    except RuntimeError:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from app import app as _app

    with _app.app_context():
        from core.models import db

        # ONLY columns that are new — no profile_pic, no subscription_end
        COLUMNS = [
            ("preferred_location", "VARCHAR(255)", "DEFAULT NULL"),
            ("work_type",          "VARCHAR(255)", "DEFAULT NULL"),
            ("employment_type",    "VARCHAR(255)", "DEFAULT NULL"),
            ("salary_min",         "INTEGER",      "DEFAULT NULL"),
            ("salary_max",         "INTEGER",      "DEFAULT NULL"),
            ("skills",             "TEXT",         "DEFAULT NULL"),
            ("saved_job_ids",      "JSON",         "DEFAULT NULL"),
        ]

        conn    = db.engine.connect()
        dialect = db.engine.dialect.name
        added, skipped = [], []

        for col_name, col_type, col_default in COLUMNS:
            try:
                if dialect in ("mysql", "mariadb"):
                    count = conn.execute(db.text(
                        "SELECT COUNT(*) FROM information_schema.columns "
                        "WHERE table_schema = DATABASE() "
                        "  AND table_name = 'users' "
                        "  AND column_name = :col"
                    ), {"col": col_name}).scalar()
                    if count:
                        skipped.append(col_name)
                        print(f"  – Skipped : {col_name} (already exists)")
                        continue
                    conn.execute(db.text(
                        f"ALTER TABLE `users` ADD COLUMN `{col_name}` {col_type} {col_default}"
                    ))
                    conn.commit()

                elif dialect == "sqlite":
                    rows = conn.execute(db.text("PRAGMA table_info(users)")).fetchall()
                    if col_name in [r[1] for r in rows]:
                        skipped.append(col_name)
                        print(f"  – Skipped : {col_name} (already exists)")
                        continue
                    conn.execute(db.text(
                        f"ALTER TABLE users ADD COLUMN {col_name} {col_type} {col_default}"
                    ))

                elif dialect == "postgresql":
                    conn.execute(db.text(
                        f'ALTER TABLE "users" ADD COLUMN IF NOT EXISTS {col_name} {col_type}'
                    ))
                    conn.commit()

                added.append(col_name)
                print(f"  ✓ Added   : {col_name} ({col_type})")

            except Exception as exc:
                skipped.append(col_name)
                print(f"  – Skipped : {col_name}  ({exc})")

        try:
            conn.commit()
        except Exception:
            pass
        conn.close()
        print(f"\nDone. Added {len(added)}, skipped {len(skipped)}.")
        if added:   print("  Added   :", ", ".join(added))
        if skipped: print("  Skipped :", ", ".join(skipped))

if __name__ == "__main__":
    run()