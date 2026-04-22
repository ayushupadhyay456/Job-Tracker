import os
import re
import hashlib
import time
import threading
import pymysql
import requests as http_requests
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
from flask_migrate import Migrate
from flask_login import LoginManager, login_user, login_required, current_user

from core.models import db, User, OneClickApplication
from core.billing import billing_bp
from core.razorpay import get_checkout_url
from core.resume_parser import extract_text_from_pdf, truncate_resume, parse_resume_sections, infer_role_from_resume
from core.jobs_service import get_jobs_for_user
from core.scorer import score_resume_against_job, score_resume_ats, analyze_job_fit
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

pymysql.install_as_MySQLdb()

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

app.config['SQLALCHEMY_DATABASE_URI']        = os.getenv("DATABASE_URL")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY']                     = os.getenv("FLASK_SECRET", "dev-key-2026")
app.config['MAX_CONTENT_LENGTH']             = 5 * 1024 * 1024   # 5 MB

db.init_app(app)
migrate = Migrate(app, db)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'signup_view'

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

app.register_blueprint(billing_bp)


# ─────────────────────────────────────────────────────────────────────────────
# MODULE-LEVEL GEMINI SINGLETON
# Initialised once at startup — NOT inside request handlers.
# This eliminates the per-request genai.Client() construction that was
# happening in _generate_cover_letter() on every apply preview call.
# ─────────────────────────────────────────────────────────────────────────────

_GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY")
_gemini_app_client = None
if _GEMINI_API_KEY:
    try:
        from google import genai as _genai
        _gemini_app_client = _genai.Client(api_key=_GEMINI_API_KEY)
    except Exception as _e:
        print(f"⚠️  Gemini init failed in app.py: {_e}")


# ─────────────────────────────────────────────────────────────────────────────
# COVER LETTER CACHE
# Caches generated cover letters per (user_id, job_title, company) for 1 hour.
# Same user clicking "Apply" on the same job twice → no API call.
# ─────────────────────────────────────────────────────────────────────────────

_cl_cache: dict  = {}
_cl_lock         = threading.Lock()
CL_CACHE_TTL     = 3600   # 1 hour


def _cl_cache_key(user_id: int, job_title: str, company: str, job_description: str = "") -> str:
    raw = f"{user_id}:{job_title.lower().strip()}:{company.lower().strip()}:{job_description[:200]}"
    return hashlib.md5(raw.encode()).hexdigest()[:20]


# ─────────────────────────────────────────────────────────────────────────────
# JINJA2 GLOBALS
# ─────────────────────────────────────────────────────────────────────────────

import time as _time

@app.template_global()
def now_timestamp():
    return _time.time()

@app.template_global()
def plan_total_days(plan: str) -> int:
    plan = (plan or '').lower().strip()
    if plan in ('yearly', 'annual'):
        return 365
    if plan in ('biannual', 'bi-annual', '6month', 'semi-annual'):
        return 183
    return 30


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _redirect_for_user(user):
    if not user.onboarding_complete:
        return redirect(url_for('onboarding'))
    if user.subscription_status != 'active':
        checkout_url = get_checkout_url(user.plan or 'monthly', user.email, user.name)
        return redirect(checkout_url)
    return redirect(url_for('jobs'))


def _require_active_subscription():
    if not current_user.onboarding_complete:
        return redirect(url_for('onboarding'))
    if current_user.subscription_status != 'active':
        flash('Please complete your subscription to access this page.', 'error')
        checkout_url = get_checkout_url(
            current_user.plan or 'monthly',
            current_user.email,
            current_user.name,
        )
        return redirect(checkout_url)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# ATS DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def detect_ats(url: str) -> dict:
    if not url:
        return {"ats": "redirect", "url": url or ""}

    gh = re.search(r'greenhouse\.io/([^/]+)/jobs/(\d+)', url)
    if gh:
        return {"ats": "greenhouse", "board_token": gh.group(1), "job_id": gh.group(2)}

    lv = re.search(r'lever\.co/([^/]+)/([a-f0-9-]{36})', url)
    if lv:
        return {"ats": "lever", "company": lv.group(1), "posting_id": lv.group(2)}

    wk = re.search(r'workable\.com/([^/]+)/j/([A-Za-z0-9]+)', url)
    if wk:
        return {"ats": "workable", "company": wk.group(1), "job_id": wk.group(2)}

    rc = re.search(r'([^.]+)\.recruitee\.com/o/([^/?]+)', url)
    if rc:
        return {"ats": "recruitee", "company": rc.group(1), "slug": rc.group(2)}

    return {"ats": "redirect", "url": url}


# ─────────────────────────────────────────────────────────────────────────────
# ATS SUBMIT
# ─────────────────────────────────────────────────────────────────────────────

def _submit_greenhouse(board_token: str, job_id: str, user, cover_letter: str) -> dict:
    endpoint  = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{job_id}"
    form_data = {
        "first_name":   user.name.split()[0] if user.name else "",
        "last_name":    " ".join(user.name.split()[1:]) if user.name and len(user.name.split()) > 1 else "",
        "email":        user.email,
        "resume_text":  user.resume_text or "",
        "cover_letter": cover_letter or "",
    }
    try:
        resp = http_requests.post(endpoint, data=form_data, timeout=12)
        if resp.status_code in (200, 201):
            return {"ok": True, "ats": "greenhouse"}
        return {"ok": False, "error": f"Greenhouse returned {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _submit_lever(company: str, posting_id: str, user, cover_letter: str) -> dict:
    endpoint = "https://api.lever.co/v0/postings/{company}/{posting_id}/apply".format(
        company=company, posting_id=posting_id
    )
    payload = {
        "name":     user.name or "",
        "email":    user.email,
        "resume":   user.resume_text or "",
        "comments": cover_letter or "",
    }
    try:
        resp = http_requests.post(endpoint, json=payload, timeout=12)
        if resp.status_code in (200, 201):
            return {"ok": True, "ats": "lever"}
        return {"ok": False, "error": f"Lever returned {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _submit_workable(company: str, job_id: str, user, cover_letter: str) -> dict:
    endpoint = f"https://apply.workable.com/api/v1/widget/accounts/{company}/jobs/{job_id}/apply"
    payload  = {
        "firstname": user.name.split()[0] if user.name else "",
        "lastname":  " ".join(user.name.split()[1:]) if user.name and len(user.name.split()) > 1 else "",
        "email":     user.email,
        "summary":   cover_letter or "",
        "resume":    user.resume_text or "",
    }
    try:
        resp = http_requests.post(endpoint, json=payload, timeout=12)
        if resp.status_code in (200, 201):
            return {"ok": True, "ats": "workable"}
        return {"ok": False, "error": f"Workable returned {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _submit_recruitee(company: str, slug: str, user, cover_letter: str) -> dict:
    endpoint = f"https://{company}.recruitee.com/api/v1/candidates"
    payload  = {
        "candidate": {
            "name":         user.name or "",
            "email":        user.email,
            "cover_letter": cover_letter or "",
        },
        "offers": [{"slug": slug}],
    }
    try:
        resp = http_requests.post(endpoint, json=payload, timeout=12)
        if resp.status_code in (200, 201):
            return {"ok": True, "ats": "recruitee"}
        return {"ok": False, "error": f"Recruitee returned {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _attempt_ats_submit(job_url: str, user, cover_letter: str) -> dict:
    ats_info = detect_ats(job_url)
    ats      = ats_info.get("ats")

    if ats == "greenhouse":
        result = _submit_greenhouse(ats_info["board_token"], ats_info["job_id"], user, cover_letter)
        result["mode"] = "direct" if result["ok"] else "redirect"
        if not result["ok"]:
            result["url"] = job_url
        return result

    if ats == "lever":
        result = _submit_lever(ats_info["company"], ats_info["posting_id"], user, cover_letter)
        result["mode"] = "direct" if result["ok"] else "redirect"
        if not result["ok"]:
            result["url"] = job_url
        return result

    if ats == "workable":
        result = _submit_workable(ats_info["company"], ats_info["job_id"], user, cover_letter)
        result["mode"] = "direct" if result["ok"] else "redirect"
        if not result["ok"]:
            result["url"] = job_url
        return result

    if ats == "recruitee":
        result = _submit_recruitee(ats_info["company"], ats_info["slug"], user, cover_letter)
        result["mode"] = "direct" if result["ok"] else "redirect"
        if not result["ok"]:
            result["url"] = job_url
        return result

    return {"ok": False, "mode": "redirect", "url": job_url, "ats": ats or "unknown"}


# ─────────────────────────────────────────────────────────────────────────────
# LANDING
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('landing.html')


# ─────────────────────────────────────────────────────────────────────────────
# PROFILE PICTURE
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/upload-pfp', methods=['POST'])
@login_required
def upload_pfp():
    if 'pfp' not in request.files:
        return redirect(request.referrer or url_for('jobs'))
    file = request.files['pfp']
    if file.filename == '':
        return redirect(request.referrer or url_for('jobs'))

    allowed = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in allowed:
        flash('Please upload a valid image (PNG, JPG, GIF, WEBP).', 'error')
        return redirect(request.referrer or url_for('jobs'))

    upload_dir = '/tmp/uploads' if os.getenv('VERCEL') else 'static/uploads'
    os.makedirs(upload_dir, exist_ok=True)
    filename = secure_filename(f"pfp_{current_user.id}.{ext}")
    file.save(os.path.join(upload_dir, filename))
    current_user.profile_pic = f'/static/uploads/{filename}'
    db.session.commit()
    return redirect(request.referrer or url_for('jobs'))


# ─────────────────────────────────────────────────────────────────────────────
# AUTH — Google OAuth
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/auth/google')
def auth_google():
    from core.oauth import get_flow
    from flask import session
    flow = get_flow(url_for('auth_google_callback', _external=True))
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='select_account',
    )
    session['oauth_state'] = state
    return redirect(authorization_url)


@app.route('/auth/google/callback')
def auth_google_callback():
    from core.oauth import get_flow, get_user_info
    from flask import session
    flow = get_flow(url_for('auth_google_callback', _external=True))
    flow.fetch_token(authorization_response=request.url)

    if session.get('oauth_state') != request.args.get('state'):
        flash('OAuth state mismatch. Please try again.', 'error')
        return redirect(url_for('signup_view'))

    info = get_user_info(flow.credentials)
    user = User.query.filter_by(email=info['email']).first()

    if not user:
        plan = session.pop('signup_plan', 'monthly')
        user = User(
            name                = info['name'],
            email               = info['email'],
            google_id           = info['google_id'],
            picture             = info['picture'],
            subscription_status = 'pending_payment',
            plan                = plan,
            onboarding_complete = False,
        )
        db.session.add(user)
        db.session.commit()
        login_user(user)
        return redirect(url_for('onboarding'))
    else:
        user.google_id = info['google_id']
        user.picture   = info['picture']
        if not user.name:
            user.name = info['name']
        db.session.commit()
        login_user(user)
        return _redirect_for_user(user)


# ─────────────────────────────────────────────────────────────────────────────
# AUTH — Email / password
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/signup', methods=['GET'])
def signup_view():
    return render_template('signup.html')


@app.route('/api/signup', methods=['POST'])
def api_signup():
    try:
        data     = request.get_json()
        name     = data.get('name', '').strip()
        email    = data.get('email', '').lower().strip()
        password = data.get('password', '')
        plan     = data.get('plan', 'monthly')

        if not name or not email or not password:
            return jsonify({"success": False, "error": "Name, email and password are required."}), 400

        if User.query.filter_by(email=email).first():
            return jsonify({
                "success": False,
                "error": "An account with this email already exists. Please log in instead.",
            }), 409

        user = User(
            name                = name,
            email               = email,
            subscription_status = 'pending_payment',
            plan                = plan,
            onboarding_complete = False,
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        login_user(user)
        return jsonify({"success": True, "redirect_url": url_for('onboarding')})

    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Signup Error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email    = request.form.get('email', '').lower().strip()
        password = request.form.get('password', '')
        remember = bool(request.form.get('remember'))

        user = User.query.filter_by(email=email).first()
        if not user or not user.check_password(password):
            flash('Invalid email or password.', 'error')
            return redirect(url_for('login'))

        login_user(user, remember=remember)
        next_page = request.args.get('next')
        if next_page and next_page.startswith('/'):
            return redirect(next_page)
        return _redirect_for_user(user)

    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    from flask_login import logout_user
    logout_user()
    flash('You have been logged out.', 'success')
    return redirect(url_for('index'))


# ─────────────────────────────────────────────────────────────────────────────
# ONBOARDING
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/onboarding')
@login_required
def onboarding():
    if current_user.onboarding_complete:
        return _redirect_for_user(current_user)
    return render_template('onboarding.html')


@app.route('/onboarding/save-preferences', methods=['POST'])
@login_required
def onboarding_save_preferences():
    data = request.get_json() or {}

    role = data.get('role', '').strip()
    exp  = data.get('experience_level', '').strip()
    if role:
        current_user.inferred_role = role
        current_user.domain        = role
    if exp:
        current_user.experience_level = exp
    if data.get('preferred_location'):
        current_user.preferred_location = data['preferred_location'].strip()
    if data.get('work_type'):
        current_user.work_type = data['work_type']
    if data.get('employment_type'):
        current_user.employment_type = data['employment_type']
    if data.get('salary_min'):
        try: current_user.salary_min = int(data['salary_min'])
        except (ValueError, TypeError): pass
    if data.get('salary_max'):
        try: current_user.salary_max = int(data['salary_max'])
        except (ValueError, TypeError): pass
    if data.get('skills'):
        current_user.skills = data['skills']

    db.session.commit()
    return jsonify({"success": True})


@app.route('/onboarding/upload-resume', methods=['POST'])
@login_required
def onboarding_upload_resume():
    file = request.files.get('resume_pdf')
    if not file or file.filename == '':
        return jsonify({"success": False, "error": "No file provided"}), 400
    if not file.filename.lower().endswith('.pdf'):
        return jsonify({"success": False, "error": "Only PDF files are supported"}), 400

    file_bytes = file.read()
    if len(file_bytes) > 5 * 1024 * 1024:
        return jsonify({"success": False, "error": "File too large (max 5 MB)"}), 400

    try:
        raw_text = extract_text_from_pdf(file_bytes)
        if not raw_text.strip():
            return jsonify({
                "success": False,
                "error": "Could not extract text. Please upload a text-based PDF.",
            }), 422

        parsed_data = parse_resume_sections(raw_text)
        current_user.resume_text   = truncate_resume(raw_text, max_chars=5000)
        current_user.inferred_role = parsed_data.get('role') or infer_role_from_resume(raw_text)
        current_user.domain        = current_user.inferred_role

        pdf_name = parsed_data.get('name', '')
        if pdf_name and pdf_name != 'Unknown' and not current_user.name:
            current_user.name = pdf_name

        db.session.commit()
        return jsonify({"success": True, "detected_role": current_user.inferred_role})

    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Onboarding resume parse error: {e}")
        return jsonify({"success": False, "error": "Failed to parse resume"}), 500


@app.route('/onboarding/complete', methods=['POST'])
@login_required
def onboarding_complete():
    current_user.onboarding_complete = True
    db.session.commit()

    checkout_url = get_checkout_url(
        current_user.plan or 'monthly',
        current_user.email,
        current_user.name,
    )
    return jsonify({"success": True, "redirect_url": checkout_url})


# ─────────────────────────────────────────────────────────────────────────────
# JOBS  (paid users only)
# ─────────────────────────────────────────────────────────────────────────────

def _flatten_field(val):
    """Normalise a DB field that may be a Python list, JSON-encoded list, or plain string."""
    import json as _json
    if not val:
        return ''
    if isinstance(val, list):
        return ', '.join(str(v) for v in val if v)
    if isinstance(val, str):
        try:
            parsed = _json.loads(val)
            if isinstance(parsed, list):
                return ', '.join(str(v) for v in parsed if v)
        except (ValueError, TypeError):
            pass
        return val
    return str(val)


def _build_plan_info(user):
    """
    Build the plan_info dict consumed by jobs.html.
    Tries to import a helper from core.billing; falls back to a minimal
    dict derived directly from the User model so the template always works.
    """
    try:
        from core.billing import get_plan_info  # may not exist in all versions
        return get_plan_info(user)
    except Exception:
        pass

    import datetime
    plan  = (user.plan or 'monthly').lower()
    label_map = {
        'monthly':  ('Monthly',  '#1849a9'),
        'yearly':   ('Yearly',   '#0F6E56'),
        'annual':   ('Annual',   '#0F6E56'),
        'biannual': ('6-Month',  '#854F0B'),
    }
    label, colour = label_map.get(plan, ('Free', '#6c6560'))

    days_left = None
    ends_str  = None
    if user.subscription_ends:
        try:
            end_dt    = user.subscription_ends if hasattr(user.subscription_ends, 'date') \
                        else datetime.datetime.fromisoformat(str(user.subscription_ends))
            delta     = (end_dt.date() - datetime.date.today()).days
            days_left = max(delta, 0)
            ends_str  = end_dt.strftime('%b %d, %Y')
        except Exception:
            pass

    return {
        'label':            label,
        'colour':           colour,
        'status':           user.subscription_status or 'inactive',
        'score_limit':      '∞' if plan in ('yearly', 'annual') else '50',
        'ends':             ends_str,
        'days_left':        days_left,
        'has_ats':          bool(user.ats_improved_text),
        'ats_score_before': user.ats_original_score,
        'ats_score_after':  user.ats_improved_score,
    }

@app.route('/jobs')
@login_required
def jobs():
    guard = _require_active_subscription()
    if guard:
        return guard

    has_resume = bool(current_user.resume_text)

    # ── Role guard: always derive role from the resume itself so sales/biz roles
    #    never pollute results for a software engineer.
    #    We trust inferred_role (set at upload time) over the free-text domain field.
    _ALLOWED_TECH_ROLES = {
        "Software Engineer", "Backend Developer", "Frontend Developer",
        "Full Stack Developer", "Mobile Developer", "Data Scientist",
        "Data Analyst", "Machine Learning Engineer", "DevOps Engineer",
        "Cloud Engineer", "Security Engineer", "Product Manager", "UX Designer",
    }
    raw_role = current_user.inferred_role or current_user.domain or ""
    # If stored role isn't in our allow-list, re-infer from the resume text
    if raw_role not in _ALLOWED_TECH_ROLES and current_user.resume_text:
        raw_role = infer_role_from_resume(current_user.resume_text)
    role = raw_role or None

    # Build user_prefs for the preferences panel (always shown)
    user_prefs = {
        'role':               role or '',
        'experience_level':   current_user.experience_level or '',
        'preferred_location': current_user.preferred_location or '',
        'salary_min':         current_user.salary_min or '',
        'salary_max':         current_user.salary_max or '',
        'work_type':          _flatten_field(current_user.work_type),
        'employment_type':    _flatten_field(current_user.employment_type),
    }

    # Build plan_info for the plan card
    plan_info = _build_plan_info(current_user)

    if not has_resume or not role:
        return render_template('jobs.html', jobs=[], has_resume=False,
                               user_prefs=user_prefs, skills_list=[],
                               plan_info=plan_info)

    live_jobs = get_jobs_for_user(
        role        = role,
        resume_text = current_user.resume_text,
        top_n       = 10,   # 10 fresh daily matches
        use_gemini  = False,
    )

    # Extract skills from resume for the skills panel
    skills_list = []
    try:
        parsed_resume = parse_resume_sections(current_user.resume_text)
        raw_skills    = parsed_resume.get('skills') or ''
        if isinstance(raw_skills, list):
            skills_list = [s.strip() for s in raw_skills if s.strip()]
        elif isinstance(raw_skills, str):
            skills_list = [s.strip() for s in re.split(r'[,\n•|/]', raw_skills) if s.strip()]
    except Exception:
        pass

    # Ensure salary is numeric so Jinja2 {:,.0f} formatting works
    for job in live_jobs:
        raw = job.get('salary')
        if raw is not None:
            try:
                job['salary'] = float(raw)
            except (ValueError, TypeError):
                job['salary'] = None

    return render_template('jobs.html', jobs=live_jobs, has_resume=True,
                           user_prefs=user_prefs, skills_list=skills_list,
                           plan_info=plan_info)


@app.route('/jobs/update-prefs', methods=['POST'])
@login_required
def jobs_update_prefs():
    """Update job-search preferences from the jobs dashboard."""
    guard = _require_active_subscription()
    if guard:
        return jsonify({"ok": False, "error": "Subscription required"}), 403

    data = request.get_json() or {}
    if data.get('role'):
        current_user.inferred_role = data['role'].strip()
        current_user.domain        = data['role'].strip()
    if data.get('experience_level'):
        current_user.experience_level = data['experience_level'].strip()
    if 'preferred_location' in data:
        current_user.preferred_location = data['preferred_location'].strip()
    if 'salary_min' in data:
        try: current_user.salary_min = int(data['salary_min']) if data['salary_min'] else None
        except (ValueError, TypeError): pass
    if 'salary_max' in data:
        try: current_user.salary_max = int(data['salary_max']) if data['salary_max'] else None
        except (ValueError, TypeError): pass
    if data.get('work_type'):
        current_user.work_type = data['work_type']
    if data.get('employment_type'):
        current_user.employment_type = data['employment_type']

    db.session.commit()
    return jsonify({"ok": True})


@app.route('/jobs/save', methods=['POST'])
@login_required
def jobs_save():
    guard = _require_active_subscription()
    if guard:
        return jsonify({"ok": False, "error": "Subscription required"}), 403

    data   = request.get_json() or {}
    job_id = str(data.get('job_id', ''))
    saved  = bool(data.get('saved', True))

    if not job_id:
        return jsonify({"ok": False, "error": "No job_id"}), 400

    ids = list(current_user.saved_job_ids or [])
    if saved and job_id not in ids:
        ids.append(job_id)
    elif not saved and job_id in ids:
        ids.remove(job_id)

    current_user.saved_job_ids = ids
    db.session.commit()
    return jsonify({"ok": True, "saved": saved})


# ─────────────────────────────────────────────────────────────────────────────
# ONE-CLICK APPLY
# ─────────────────────────────────────────────────────────────────────────────

def _generate_cover_letter(user, job_title: str, company: str, job_description: str) -> str:
    """
    Generates a short cover letter using Gemini.

    OPTIMISATIONS vs original:
      - Reuses module-level _gemini_app_client (no per-request genai.Client())
      - Cached per (user_id, job_title, company) for 1 hour
      - Resume truncated to 1000 chars (was 2000)
      - JD truncated to 500 chars (was 800)
      - max_output_tokens 300 (was 400)
    """
    # ── Cache check ────────────────────────────────────────────────────────────
    ck    = _cl_cache_key(user.id, job_title, company, job_description)
    entry = _cl_cache.get(ck)
    if entry and (time.time() - entry["ts"]) < CL_CACHE_TTL:
        return entry["text"]

    # ── Gemini call ────────────────────────────────────────────────────────────
    if _gemini_app_client:
        try:
            from google.genai import types as gtypes
            prompt = (
                f"Write a concise 3-paragraph (~120 word) cover letter.\n"
                f"Role: {job_title} at {company}\n"
                f"Rules: mention role+company in para 1; reference 1-2 JD requirements in para 2; "
                f"brief forward-looking close in para 3. No generic filler. Output body only, no salutation/sign-off.\n"
                f"JD: {job_description[:300]}\n"
                f"Resume: {(user.resume_text or '')[:600]}"
            )
            resp = _gemini_app_client.models.generate_content(
                model    = "gemini-2.0-flash",
                contents = prompt,
                config   = gtypes.GenerateContentConfig(
                    max_output_tokens = 220,
                    temperature       = 0.4,
                ),
            )
            text = resp.text.strip()
            with _cl_lock:
                _cl_cache[ck] = {"ts": time.time(), "text": text}
            return text
        except Exception as e:
            app.logger.warning(f"Cover letter generation failed: {e}")

    # ── Smart fallback template ────────────────────────────────────────────────
    resume_snippet = (user.resume_text or "")[:600]
    skills_preview = ""
    m = re.search(r'skills?\W+(.{30,80})', resume_snippet, re.IGNORECASE)
    if m:
        skills_preview = m.group(1).split('\n')[0].strip()

    jd_snippet = ""
    if job_description:
        jd_m = re.search(r'(?:require|seeking|looking for|must have|experience with)[^\n.]{10,60}', job_description, re.IGNORECASE)
        if jd_m:
            jd_snippet = jd_m.group(0).strip()

    text = (
        f"The {job_title} role at {company} caught my attention immediately — "
        f"it maps closely to the work I've been doing"
        + (f", particularly around {skills_preview}" if skills_preview else "")
        + ".\n\n"
        + (f"Your focus on {jd_snippet} aligns directly with my experience. " if jd_snippet else "")
        + "Throughout my career I have delivered consistent results by combining technical depth with "
        "strong cross-functional collaboration. I take pride in writing clean, maintainable solutions "
        "to complex problems and thrive in fast-paced, high-ownership environments.\n\n"
        f"I'd welcome the chance to bring this experience to {company} and contribute meaningfully "
        "to your team's goals. Thank you for considering my application."
    )
    with _cl_lock:
        _cl_cache[ck] = {"ts": time.time(), "text": text}
    return text


@app.route('/jobs/apply/preview', methods=['POST'])
@login_required
def jobs_apply_preview():
    guard = _require_active_subscription()
    if guard:
        return jsonify({"ok": False, "error": "Subscription required"}), 403

    data        = request.get_json() or {}
    job_title   = (data.get('job_title')   or '').strip()
    company     = (data.get('company')     or '').strip()
    description = (data.get('description') or '').strip()
    job_url     = (data.get('job_url')     or '').strip()

    if not job_title or not company:
        return jsonify({"ok": False, "error": "job_title and company are required"}), 400

    cover_letter = _generate_cover_letter(current_user, job_title, company, description)

    ats_info = detect_ats(job_url)
    ats      = ats_info.get("ats", "redirect")
    mode     = "direct" if ats in ("greenhouse", "lever", "workable", "recruitee") else "redirect"

    return jsonify({
        "ok":           True,
        "cover_letter": cover_letter,
        "ats":          ats,
        "apply_mode":   mode,
    })


@app.route('/jobs/apply/confirm', methods=['POST'])
@login_required
def jobs_apply_confirm():
    guard = _require_active_subscription()
    if guard:
        return jsonify({"ok": False, "error": "Subscription required"}), 403

    data         = request.get_json() or {}
    job_title    = (data.get('job_title')    or '').strip()
    company      = (data.get('company')      or '').strip()
    job_id       = (data.get('job_id')       or '').strip() or None
    job_url      = (data.get('job_url')      or '').strip() or None
    location     = (data.get('location')     or '').strip() or None
    source       = (data.get('source')       or '').strip() or None
    match_score  = data.get('match_score')
    cover_letter = (data.get('cover_letter') or '').strip() or None

    if not job_title or not company:
        return jsonify({"ok": False, "error": "job_title and company are required"}), 400

    # ── Prevent duplicate applications ────────────────────────────────────────
    existing = OneClickApplication.query.filter_by(
        user_id   = current_user.id,
        job_title = job_title,
        company   = company,
    ).first()
    if existing:
        return jsonify({
            "ok":              True,
            "already_applied": True,
            "applied_at":      existing.applied_at.isoformat(),
        })

    # ── Attempt ATS direct submit ──────────────────────────────────────────────
    submit_result = {"ok": False, "mode": "redirect", "url": job_url}
    if job_url:
        submit_result = _attempt_ats_submit(job_url, current_user, cover_letter or "")

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
        "ok":           True,
        "id":           application.id,
        "applied_at":   application.applied_at.isoformat(),
        "mode":         submit_result.get("mode", "redirect"),
        "ats":          submit_result.get("ats", "unknown"),
        "redirect_url": submit_result.get("url") if submit_result.get("mode") == "redirect" else None,
        "direct_ok":    submit_result.get("ok", False),
    })


@app.route('/jobs/applications', methods=['GET'])
@login_required
def jobs_applications():
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


@app.route('/jobs/applications/<int:app_id>/status', methods=['PATCH'])
@login_required
def jobs_application_status(app_id):
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
        return jsonify({
            "ok":    False,
            "error": f"Invalid status. Choose from: {', '.join(sorted(VALID_STATUSES))}",
        }), 400

    application.status = new_status
    db.session.commit()
    return jsonify({"ok": True, "status": new_status})


# ─────────────────────────────────────────────────────────────────────────────
# RESUME  (paid users only)
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/resume', methods=['GET', 'POST'])
@login_required
def resume():
    guard = _require_active_subscription()
    if guard:
        return guard

    if request.method == 'POST':
        file = request.files.get('resume_pdf')

        if not file or file.filename == '':
            flash('No file selected. Please choose a PDF.', 'error')
            return redirect(url_for('resume'))

        if not file.filename.lower().endswith('.pdf'):
            flash('Only PDF files are supported.', 'error')
            return redirect(url_for('resume'))

        file_bytes = file.read()
        if len(file_bytes) > 5 * 1024 * 1024:
            flash('File too large. Please upload a PDF under 5 MB.', 'error')
            return redirect(url_for('resume'))

        try:
            raw_text = extract_text_from_pdf(file_bytes)
            if not raw_text.strip():
                flash(
                    'Could not extract text. Make sure it is a text-based PDF, not a scanned image.',
                    'error',
                )
                return redirect(url_for('resume'))

            parsed_data = parse_resume_sections(raw_text)

            current_user.resume_text   = truncate_resume(raw_text, max_chars=5000)
            current_user.inferred_role = parsed_data.get('role') or infer_role_from_resume(raw_text)
            current_user.domain        = current_user.inferred_role

            pdf_name = parsed_data.get('name', '')
            if pdf_name and pdf_name != 'Unknown' and not current_user.name:
                current_user.name = pdf_name

            db.session.commit()
            flash(f'Resume uploaded! Detected role: {current_user.inferred_role}', 'success')
            return redirect(url_for('resume'))

        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Resume parse error: {e}")
            flash('Something went wrong while parsing your resume. Please try again.', 'error')
            return redirect(url_for('resume'))

    # GET — score_resume_ats is now cached in scorer.py (TTL 1 hour)
    parsed = None
    if current_user.resume_text:
        parsed = parse_resume_sections(current_user.resume_text)

        ats = score_resume_ats(
            resume_text = current_user.resume_text,
            role        = current_user.inferred_role or "",
        )
        parsed['match_score']       = ats['overall']
        parsed['technical_skills']  = ats['technical_skills']
        parsed['experience_score']  = ats['experience']
        parsed['keywords_score']    = ats['keywords']
        parsed['formatting_score']  = ats['formatting']
        parsed['missing_keywords']  = ats['missing_keywords']
        parsed['bullet_point_fix']  = ats['bullet_point_fix']
        parsed['ats_summary']       = ats['summary']
        parsed['missing_details']   = ats.get('missing_details', [])

    return render_template('resume.html', parsed=parsed)


@app.route('/resume/delete', methods=['POST'])
@login_required
def remove_resume():
    current_user.resume_text   = None
    current_user.inferred_role = None
    current_user.domain        = None
    db.session.commit()
    flash('Resume removed successfully.', 'success')
    return redirect(url_for('resume'))


# ─────────────────────────────────────────────────────────────────────────────
# RESUME IMPROVE
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/resume/improve', methods=['POST'])
@login_required
def resume_improve():
    guard = _require_active_subscription()
    if guard:
        return jsonify({"ok": False, "error": "Subscription required"}), 403

    if not current_user.resume_text:
        return jsonify({"ok": False, "error": "No resume uploaded"}), 400

    gemini_client = _gemini_app_client
    if not gemini_client:
        try:
            from core.scorer import _client as _scorer_client
            gemini_client = _scorer_client
        except Exception:
            gemini_client = None

    if not gemini_client:
        return jsonify({"ok": False, "error": "AI service not available. Please contact support."}), 503

    IMPROVE_PROMPT = (
        "Rewrite this resume to maximise ATS score for role: {role}\n"
        "Rules: strong action verbs on every bullet; quantify with * for estimates; "
        "UPPERCASE section headers; add 2-line summary if missing; keep all facts accurate; use • bullets.\n"
        "Return ONLY the improved resume text, no commentary.\n\nRESUME:\n{resume}"
    )

    try:
        from google.genai import types as gtypes

        prompt = IMPROVE_PROMPT.format(
            role   = current_user.inferred_role or "Software Engineer",
            resume = current_user.resume_text[:1800],
        )
        resp = gemini_client.models.generate_content(
            model    = "gemini-2.0-flash",
            contents = prompt,
            config   = gtypes.GenerateContentConfig(
                max_output_tokens = 900,
                temperature       = 0.3,
            ),
        )
        improved_text = resp.text.strip()

        current_user.ats_improved_text = improved_text
        db.session.commit()

        return jsonify({"ok": True, "download_url": "/resume/download-improved"})

    except Exception as e:
        err_str = str(e)
        app.logger.error(f"Resume improve error: {err_str}")
        # Show a friendly message for quota errors instead of the raw API dump
        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
            msg = "Daily AI quota reached. Please try again tomorrow, or contact support to upgrade your API plan."
        elif "API_KEY" in err_str or "authentication" in err_str.lower():
            msg = "AI service configuration error. Please contact support."
        else:
            msg = "Resume improvement failed. Please try again in a few minutes."
        return jsonify({"ok": False, "error": msg}), 500

    except Exception as e:
        app.logger.error(f"Resume improve error: {e}")
        return jsonify({"ok": False, "error": "Improvement failed. Please try again."}), 500


@app.route('/resume/download-improved')
@login_required
def resume_download_improved():
    guard = _require_active_subscription()
    if guard:
        return redirect(url_for('resume'))

    from flask import make_response
    # Read from DB — cookie session cannot hold resume-sized text (>4KB limit)
    improved_text = current_user.ats_improved_text
    if not improved_text:
        flash('No improved resume found. Please generate one first.', 'error')
        return redirect(url_for('resume'))

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.lib import colors
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
        from reportlab.lib.enums import TA_LEFT, TA_CENTER
        import io

        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf,
            pagesize     = A4,
            rightMargin  = 2   * cm,
            leftMargin   = 2   * cm,
            topMargin    = 1.8 * cm,
            bottomMargin = 1.8 * cm,
        )

        styles = getSampleStyleSheet()

        name_style = ParagraphStyle(
            'ResumeName',
            parent    = styles['Normal'],
            fontSize  = 18,
            leading   = 22,
            fontName  = 'Helvetica-Bold',
            textColor = colors.HexColor('#0d0c0b'),
            spaceAfter= 2,
        )
        contact_style = ParagraphStyle(
            'ResumeContact',
            parent    = styles['Normal'],
            fontSize  = 9,
            leading   = 13,
            textColor = colors.HexColor('#6c6560'),
            spaceAfter= 8,
        )
        section_style = ParagraphStyle(
            'SectionHead',
            parent      = styles['Normal'],
            fontSize    = 10,
            leading     = 14,
            fontName    = 'Helvetica-Bold',
            textColor   = colors.HexColor('#d95215'),
            spaceBefore = 12,
            spaceAfter  = 4,
        )
        normal_style = ParagraphStyle(
            'ResumeNormal',
            parent    = styles['Normal'],
            fontSize  = 9.5,
            leading   = 14,
            textColor = colors.HexColor('#1c1a17'),
            spaceAfter= 2,
        )
        bullet_style = ParagraphStyle(
            'ResumeBullet',
            parent          = styles['Normal'],
            fontSize        = 9.5,
            leading         = 14,
            textColor       = colors.HexColor('#1c1a17'),
            leftIndent      = 12,
            firstLineIndent = -10,
            spaceAfter      = 2,
        )
        summary_style = ParagraphStyle(
            'ResumeSummary',
            parent        = styles['Normal'],
            fontSize      = 9.5,
            leading       = 15,
            textColor     = colors.HexColor('#3a3530'),
            backColor     = colors.HexColor('#f6f4f1'),
            borderPadding = (6, 8, 6, 8),
            spaceAfter    = 6,
        )

        def safe(text):
            return (text or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

        story      = []
        lines      = improved_text.splitlines()
        in_summary = False

        SECTION_RE = re.compile(
            r'^(EXPERIENCE|EDUCATION|SKILLS|PROJECTS|CERTIFICATIONS|SUMMARY|'
            r'CONTACT|PROFILE|ACHIEVEMENTS|WORK\s+HISTORY|PROFESSIONAL\s+SUMMARY|'
            r'TECHNICAL\s+SKILLS|EMPLOYMENT)\b',
            re.IGNORECASE
        )

        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                story.append(Spacer(1, 4))
                continue

            if i == 0 or (i <= 2 and not any(stripped.startswith(c) for c in ['•', '-', '*'])):
                if i == 0:
                    story.append(Paragraph(safe(stripped), name_style))
                    continue

            if SECTION_RE.match(stripped) or (stripped.isupper() and 3 < len(stripped) < 40):
                story.append(HRFlowable(width='100%', thickness=0.5,
                                        color=colors.HexColor('#e8e4df'), spaceAfter=4))
                story.append(Paragraph(safe(stripped), section_style))
                in_summary = stripped.upper() in ('SUMMARY', 'PROFESSIONAL SUMMARY', 'PROFILE', 'OBJECTIVE')
                continue

            if i <= 5 and re.search(r'@|linkedin|github|\+\d|\(\d', stripped, re.I):
                story.append(Paragraph(safe(stripped), contact_style))
                continue

            if stripped.startswith('•') or stripped.startswith('- ') or stripped.startswith('* '):
                bullet_text = stripped.lstrip('•-* ').strip()
                story.append(Paragraph(f'• {safe(bullet_text)}', bullet_style))
                continue

            if in_summary:
                story.append(Paragraph(safe(stripped), summary_style))
                continue

            story.append(Paragraph(safe(stripped), normal_style))

        doc.build(story)
        buf.seek(0)

        name_slug = re.sub(r'[^a-z0-9]', '_', (current_user.name or 'resume').lower())
        filename  = f"{name_slug}_improved_resume.pdf"

        response = make_response(buf.read())
        response.headers['Content-Type']        = 'application/pdf'
        response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    except ImportError:
        flash('PDF generation library not installed. Run: pip install reportlab', 'error')
        return redirect(url_for('resume'))
    except Exception as e:
        app.logger.error(f"PDF generation error: {e}")
        flash('Could not generate PDF. Please try again.', 'error')
        return redirect(url_for('resume'))


# ─────────────────────────────────────────────────────────────────────────────
# APPLIED JOBS
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/applied')
@login_required
def applied_jobs():
    guard = _require_active_subscription()
    if guard:
        return guard

    apps = (
        OneClickApplication.query
        .filter_by(user_id=current_user.id)
        .order_by(OneClickApplication.applied_at.desc())
        .all()
    )
    return render_template('applied.html', applications=apps)


# ─────────────────────────────────────────────────────────────────────────────
# PROFILE
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/profile')
@login_required
def profile():
    guard = _require_active_subscription()
    if guard:
        return guard

    user_prefs = {
        'role':               current_user.inferred_role or current_user.domain or '',
        'experience_level':   current_user.experience_level or '',
        'preferred_location': current_user.preferred_location or '',
        'salary_min':         current_user.salary_min or '',
        'salary_max':         current_user.salary_max or '',
        'work_type':          _flatten_field(current_user.work_type),
        'employment_type':    _flatten_field(current_user.employment_type),
    }

    skills_list = []
    if current_user.resume_text:
        try:
            parsed_resume = parse_resume_sections(current_user.resume_text)
            raw_skills    = parsed_resume.get('skills') or []
            if isinstance(raw_skills, list):
                skills_list = [s.strip() for s in raw_skills if s.strip()]
            elif isinstance(raw_skills, str):
                skills_list = [s.strip() for s in re.split(r'[,\n•|/]', raw_skills) if s.strip()]
        except Exception:
            pass

    return render_template('profile.html', user_prefs=user_prefs, skills_list=skills_list)


@app.route('/profile/update', methods=['POST'])
@login_required
def profile_update():
    guard = _require_active_subscription()
    if guard:
        return guard

    new_name = request.form.get('name', '').strip()
    if new_name:
        current_user.name = new_name
        db.session.commit()
        flash('Profile updated successfully.', 'success')
    else:
        flash('Name cannot be empty.', 'error')

    return redirect(url_for('profile'))


@app.route('/profile/change-password', methods=['POST'])
@login_required
def profile_change_password():
    guard = _require_active_subscription()
    if guard:
        return guard

    from werkzeug.security import check_password_hash, generate_password_hash

    current_pw  = request.form.get('current_password', '')
    new_pw      = request.form.get('new_password', '')
    confirm_pw  = request.form.get('confirm_password', '')

    if not check_password_hash(current_user.password_hash, current_pw):
        flash('Current password is incorrect.', 'error')
        return redirect(url_for('profile'))

    if len(new_pw) < 8:
        flash('New password must be at least 8 characters.', 'error')
        return redirect(url_for('profile'))

    if new_pw != confirm_pw:
        flash('New passwords do not match.', 'error')
        return redirect(url_for('profile'))

    current_user.password_hash = generate_password_hash(new_pw)
    db.session.commit()
    flash('Password changed successfully.', 'success')
    return redirect(url_for('profile'))


@app.route('/profile/delete-account', methods=['POST'])
@login_required
def profile_delete_account():
    from flask_login import logout_user
    user = current_user
    logout_user()
    # Delete applications first (FK constraint)
    OneClickApplication.query.filter_by(user_id=user.id).delete()
    db.session.delete(user)
    db.session.commit()
    flash('Your account has been deleted.', 'success')
    return redirect(url_for('signup_view'))


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC STATIC / INFORMATIONAL PAGES  (no login required)
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/about')
def about():
    return render_template('about.html')


@app.route('/terms')
def terms():
    return render_template('terms.html')


@app.route('/privacy')
def privacy():
    return render_template('privacy.html')


@app.route('/refund')
def refund():
    return render_template('refund.html')


@app.route('/contact', methods=['GET', 'POST'])
def contact():
    return render_template('contact.html', contact_sent=False)


@app.route('/blog')
def blog():
    return redirect('/')


@app.route('/blog/is-pathhire-legit')
def blog_legit():
    return render_template('blog_is_pathhire_legit.html')


@app.route('/blog/success-stories')
def blog_success():
    return render_template('blog_success_stories.html')


@app.route('/blog/top-remote-companies')
def blog_remote_companies():
    return render_template('blog_success_stories.html')   # placeholder


@app.route('/blog/career-change-guide')
def blog_career_change():
    return render_template('blog_is_pathhire_legit.html')  # placeholder


@app.route('/blog/entry-level-jobs')
def blog_entry_level():
    return render_template('blog_success_stories.html')   # placeholder


@app.route('/cover-letter', methods=['GET', 'POST'])
def cover_letter():
    return render_template('cover_letter.html')


@app.route('/tools/interview-prep', methods=['GET', 'POST'])
def interview_prep():
    return render_template('interview_prep.html')


@app.route('/tools/salary-benchmarker', methods=['GET', 'POST'])
def salary_benchmarker():
    return render_template('salary_benchmarker.html')


@app.route('/tools/linkedin-optimiser', methods=['GET', 'POST'])
def linkedin_optimiser():
    return render_template('linkedin_optimiser.html')


# ─────────────────────────────────────────────────────────────────────────────
# PRICING
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/pricing')
def pricing():
    return render_template('pricing.html')


# ─────────────────────────────────────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=5000, debug=True)