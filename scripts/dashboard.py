import sys
import os

# Add the parent directory (root) to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, render_template
from core.database import SessionLocal, JobApplication
# ... rest of your code ...

# This tells Flask to look for the 'templates' folder inside 'scripts/'
base_dir = os.path.dirname(os.path.abspath(__file__))
template_dir = os.path.join(base_dir, 'templates')

app = Flask(__name__, template_folder=template_dir)

@app.route('/')
def index():
    session = SessionLocal()
    try:
        # Fetch jobs ordered by the most recent activity
        jobs = session.query(JobApplication).order_by(JobApplication.last_activity.desc()).all()
        return render_template('dashboard.html', jobs=jobs)
    except Exception as e:
        return f"Database Error: {e}"
    finally:
        session.close()

if __name__ == "__main__":
    # Running on 0.0.0.0 for Docker compatibility
    app.run(host='0.0.0.0', port=5000, debug=True)