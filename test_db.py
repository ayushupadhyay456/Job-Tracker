from core.database import SessionLocal, JobApplication
import datetime
# Change this:
# last_activity=datetime.datetime.utcnow()

# To this:
from datetime import datetime, UTC


db = SessionLocal()

test_job = JobApplication(
    company="Google (Test)",
    email="recruiter@google.com",
    status="Interested",
    thread_id="test_thread_001",
    is_recruiter=True,
    ai_summary="Personalized outreach regarding a Senior Software Engineer role in Kolkata.",
    confidence_score=95,
    last_activity=datetime.now(UTC)
)

try:
    db.add(test_job)
    db.commit()
    print("✅ Successfully injected test recruiter lead!")
except Exception as e:
    print(f"❌ Error: {e}")
finally:
    db.close()