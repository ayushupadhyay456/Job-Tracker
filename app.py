import os
import pymysql
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
from flask_migrate import Migrate
from flask_login import LoginManager, login_user, login_required, current_user
from core.scorer import score_resume_against_job

from core.models import db, User
from core.billing import billing_bp
from core.razorpay import get_checkout_url
from core.resume_parser import extract_text_from_pdf, truncate_resume, parse_resume_sections, infer_role_from_resume
from core.jobs_service import get_jobs_for_user          # ← batch-scoring pipeline
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
    return db.session.get(User, int(user_id))   # SQLAlchemy 2.x style (no legacy warning)

app.register_blueprint(billing_bp)


# ── Landing ───────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('landing.html')


# ── Profile picture ───────────────────────────────────────────────────────────
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

    os.makedirs('static/uploads', exist_ok=True)
    filename = secure_filename(f"pfp_{current_user.id}.{ext}")
    file.save(os.path.join('static/uploads', filename))
    current_user.profile_pic = f'/static/uploads/{filename}'
    db.session.commit()
    return redirect(request.referrer or url_for('jobs'))


# ── Auth ──────────────────────────────────────────────────────────────────────
@app.route('/auth/google')
def auth_google():
    from core.oauth import get_flow
    import secrets
    flow = get_flow(url_for('auth_google_callback', _external=True))
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='select_account'
    )
    from flask import session
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
        # New user — create account, send to payment
        plan = session.pop('signup_plan', 'monthly')
        user = User(
            name=info['name'],
            email=info['email'],
            google_id=info['google_id'],
            picture=info['picture'],
            subscription_status='pending_payment',
            plan=plan
        )
        db.session.add(user)
        db.session.commit()
        login_user(user)
        redirect_url = get_checkout_url(plan, info['email'], info['name'])
        return redirect(redirect_url)
    else:
        # Existing user — update Google info and log in
        user.google_id = info['google_id']
        user.picture   = info['picture']
        if not user.name:
            user.name = info['name']
        db.session.commit()
        login_user(user)
        if user.subscription_status == 'active':
            return redirect(url_for('jobs'))
        else:
            redirect_url = get_checkout_url(user.plan, user.email, user.name)
            return redirect(redirect_url)


@app.route('/signup', methods=['GET'])
def signup_view():
    return render_template('signup.html')


@app.route('/api/signup', methods=['POST'])
def api_signup():
    try:
        data     = request.get_json()
        name     = data.get('name')
        email    = data.get('email', '').lower()
        password = data.get('password')
        plan     = data.get('plan', 'monthly')

        # ── BUG FIX: reject duplicate emails instead of silently logging in
        #    the existing account (which caused wrong name to appear in UI)
        user = User.query.filter_by(email=email).first()
        if user:
            return jsonify({
                "success": False,
                "error": "An account with this email already exists. Please log in instead."
            }), 409

        user = User(name=name, email=email,
                    subscription_status='pending_payment', plan=plan)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        login_user(user)
        redirect_url = get_checkout_url(plan, email, name)
        return jsonify({"success": True, "redirect_url": redirect_url})

    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Signup Error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ── Login ─────────────────────────────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email    = request.form.get('email', '').lower()
        password = request.form.get('password')
        remember = bool(request.form.get('remember'))

        user = User.query.filter_by(email=email).first()
        if not user or not user.check_password(password):
            flash('Invalid email or password.', 'error')
            return redirect(url_for('login'))

        login_user(user, remember=remember)
        return redirect(request.args.get('next') or url_for('jobs'))

    return render_template('login.html')


# ── Logout ────────────────────────────────────────────────────────────────────
@app.route('/logout')
@login_required
def logout():
    from flask_login import logout_user
    logout_user()
    flash('You have been logged out.', 'success')
    return redirect(url_for('index'))


# ── Onboarding ────────────────────────────────────────────────────────────────
@app.route('/onboarding')
@login_required
def onboarding():
    if current_user.onboarding_complete:
        return redirect(url_for('jobs'))
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
    db.session.commit()
    return jsonify({"success": True})


@app.route('/onboarding/complete', methods=['POST'])
@login_required
def onboarding_complete():
    current_user.onboarding_complete = True
    db.session.commit()
    return jsonify({"success": True})


# ── Jobs ──────────────────────────────────────────────────────────────────────
@app.route('/jobs')
@login_required
def jobs():
    has_resume = bool(current_user.resume_text)

    # ── BUG FIX: when the user removes their resume, inferred_role and domain
    #    are cleared too. Don't fall back to a hardcoded role — instead show
    #    an empty dashboard that prompts the user to re-upload their resume.
    role = current_user.inferred_role or current_user.domain

    if not has_resume or not role:
        # No resume → nothing to score against; skip the API call entirely
        return render_template(
            'jobs.html',
            jobs       = [],
            has_resume = False,
        )

    live_jobs = get_jobs_for_user(
        role        = role,
        resume_text = current_user.resume_text,
        top_n       = 20,
    )

    return render_template(
        'jobs.html',
        jobs       = live_jobs,
        has_resume = True,
    )


# ── Jobs: save/bookmark ───────────────────────────────────────────────────────
@app.route('/jobs/save', methods=['POST'])
@login_required
def jobs_save():
    data   = request.get_json() or {}
    job_id = str(data.get('job_id', ''))
    saved  = bool(data.get('saved', True))

    if not job_id:
        return jsonify({"ok": False, "error": "No job_id"}), 400

    # saved_job_ids is a JSON list column — add to User model if missing:
    # saved_job_ids = db.Column(db.JSON, default=list)
    ids = list(current_user.saved_job_ids or [])
    if saved and job_id not in ids:
        ids.append(job_id)
    elif not saved and job_id in ids:
        ids.remove(job_id)

    current_user.saved_job_ids = ids
    db.session.commit()
    return jsonify({"ok": True, "saved": saved})


# ── Resume ────────────────────────────────────────────────────────────────────
@app.route('/resume', methods=['GET', 'POST'])
@login_required
def resume():
    if request.method == 'POST':
        file = request.files.get('resume_pdf')
        # ... (keep existing file validation logic)

        file_bytes = file.read()
        try:
            raw_text = extract_text_from_pdf(file_bytes)
            # ... (keep empty text check)

            # 1. Parse structured data from the resume
            parsed_data = parse_resume_sections(raw_text)
            
            # 2. Update Database: Resume text and Role
            current_user.resume_text   = truncate_resume(raw_text, max_chars=5000)
            current_user.inferred_role = parsed_data['role']
            current_user.domain        = parsed_data['role']
            
            # FIX: Update the user's name in the DB from the PDF
            if parsed_data['name'] and parsed_data['name'] != "Unknown":
                current_user.name = parsed_data['name']
                
            db.session.commit()
            flash(f'Resume uploaded! Detected role: {parsed_data["role"]}', 'success')
            return redirect(url_for('resume'))

        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Resume parse error: {e}")
            flash('Something went wrong. Please try again.', 'error')
            return redirect(url_for('resume'))

    # 3. Handle GET request: Generate dynamic data for the UI
    parsed = None
    if current_user.resume_text:
        parsed = parse_resume_sections(current_user.resume_text)
        
        # FIX: Calculate a real score instead of hardcoded 75
        # We compare their resume against a target string of their own role/skills
        target_criteria = f"{current_user.inferred_role} {' '.join(parsed['skills'][:5])}"
        parsed['match_score'] = score_resume_against_job(current_user.resume_text, target_criteria)

    return render_template('resume.html', parsed=parsed)
# @login_required
# def resume():
#     if request.method == 'POST':
#         file = request.files.get('resume_pdf')

#         if not file or file.filename == '':
#             flash('No file selected. Please choose a PDF.', 'error')
#             return redirect(url_for('resume'))

#         if not file.filename.lower().endswith('.pdf'):
#             flash('Only PDF files are supported.', 'error')
#             return redirect(url_for('resume'))

#         file_bytes = file.read()
#         if len(file_bytes) > 5 * 1024 * 1024:
#             flash('File too large. Please upload a PDF under 5 MB.', 'error')
#             return redirect(url_for('resume'))

#         try:
#             raw_text = extract_text_from_pdf(file_bytes)
#             if not raw_text.strip():
#                 flash(
#                     'Could not extract text. Make sure it is a text-based PDF, not a scanned image.',
#                     'error'
#                 )
#                 return redirect(url_for('resume'))

#             current_user.resume_text   = truncate_resume(raw_text, max_chars=5000)
#             inferred                   = infer_role_from_resume(raw_text)
#             current_user.inferred_role = inferred
#             current_user.domain        = inferred
#             db.session.commit()
#             flash(f'Resume uploaded! Detected role: {inferred}', 'success')
#             return redirect(url_for('resume'))

#         except Exception as e:
#             db.session.rollback()
#             app.logger.error(f"Resume parse error: {e}")
#             flash('Something went wrong while parsing your resume. Please try again.', 'error')
#             return redirect(url_for('resume'))

#     parsed = None
#     if current_user.resume_text:
#         parsed = parse_resume_sections(current_user.resume_text)

#     return render_template('resume.html', parsed=parsed)


# ── Resume: remove ────────────────────────────────────────────────────────────
@app.route('/remove_resume', methods=['POST'])
@login_required
def remove_resume():
    current_user.resume_text   = None
    current_user.inferred_role = None
    db.session.commit()
    flash('Resume removed.', 'success')
    return redirect(url_for('resume'))


@app.route('/resume/delete', methods=['POST'])
@login_required
def resume_delete():
    current_user.resume_text   = None
    current_user.inferred_role = None
    current_user.domain        = None
    db.session.commit()
    flash('Resume removed successfully.', 'success')
    return redirect(url_for('resume'))


# ── Pricing ───────────────────────────────────────────────────────────────────
@app.route('/pricing')
def pricing():
    return render_template('pricing.html')


# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=5000, debug=True)