from core.database import SessionLocal, JobApplication

def add_test_job():
    db = SessionLocal()
    new_job = JobApplication(
        company_name="Test Company",
        email="ayushupadhyay456@gmail.com", # Change this to your email to test
        status="Pending"
    )
    db.add(new_job)
    db.commit()
    db.refresh(new_job)
    print(f"✅ Injected job for {new_job.company_name} (ID: {new_job.id})")
    db.close()

if __name__ == "__main__":
    add_test_job()