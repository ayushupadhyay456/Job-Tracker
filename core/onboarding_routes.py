# ── ADD THESE ROUTES TO app.py ────────────────────────────────────────────────
# Place them anywhere after the existing imports and before if __name__ == '__main__'


# ── Onboarding ────────────────────────────────────────────────────────────────
@app.route('/onboarding')
@login_required
def onboarding():
    # If already onboarded, skip straight to jobs
    if current_user.onboarding_complete:
        return redirect(url_for('jobs'))
    return render_template('onboarding.html')


@app.route('/onboarding/save-preferences', methods=['POST'])
@login_required
def onboarding_save_preferences():
    """Called by JS during the animated setup step."""
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
    """Marks onboarding as done so user never sees it again."""
    current_user.onboarding_complete = True
    db.session.commit()
    return jsonify({"success": True})