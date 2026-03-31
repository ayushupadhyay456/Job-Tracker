"""
core/billing.py
Flask Blueprint — Razorpay billing routes.
"""

import os
from flask import (
    Blueprint, redirect, request, jsonify,
    render_template, url_for, flash, current_app
)
from flask_login import login_required, current_user
from .razorpay import (
    create_subscription,
    verify_payment_signature,
    parse_webhook,
    dispatch_webhook,
    require_webhook_secret,
    PLAN_IDS,
)
from .models import db

billing_bp = Blueprint("billing", __name__, url_prefix="/billing")


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------
@billing_bp.route("/checkout/<plan>")
@login_required
def checkout(plan: str):
    if plan not in PLAN_IDS:
        flash("Invalid plan selected.", "danger")
        return redirect(url_for("pricing"))

    try:
        subscription = create_subscription(
            plan,
            current_user.email,
            current_user.name or "",
        )
        return render_template(
            "billing/checkout.html",
            subscription_id = subscription["id"],
            razorpay_key    = os.getenv("RAZORPAY_KEY_ID"),
            plan            = plan,
            user_email      = current_user.email,
            user_name       = current_user.name or "",
        )
    except Exception as e:
        current_app.logger.error(f"Checkout error: {e}")
        flash("Could not start checkout. Please try again.", "danger")
        return redirect(url_for("pricing"))


# ---------------------------------------------------------------------------
# Payment verification
# ---------------------------------------------------------------------------
@billing_bp.route("/verify", methods=["POST"])
@login_required
def verify():
    data = request.get_json() or {}

    sub_id    = data.get("razorpay_subscription_id", "")
    pay_id    = data.get("razorpay_payment_id", "")
    signature = data.get("razorpay_signature", "")

    if not verify_payment_signature(sub_id, pay_id, signature):
        current_app.logger.warning(f"[Razorpay] Bad signature for {current_user.email}")
        return jsonify({"success": False, "error": "Signature mismatch"}), 400

    current_user.subscription_id     = sub_id
    current_user.subscription_status = "active"
    db.session.commit()

    # ── Send to onboarding if not done yet, else jobs ────────────────────────
    if not current_user.onboarding_complete:
        redirect_to = url_for("onboarding")
    else:
        redirect_to = url_for("billing.success")

    return jsonify({"success": True, "redirect": redirect_to})


# ---------------------------------------------------------------------------
# Post-payment success page (shown only if already onboarded)
# ---------------------------------------------------------------------------
@billing_bp.route("/success")
def success():
    return render_template("billing/success.html")


# ---------------------------------------------------------------------------
# Portal / Change plan / Webhook
# ---------------------------------------------------------------------------
@billing_bp.route("/portal")
@login_required
def portal():
    return render_template("billing/portal.html", user=current_user)


@billing_bp.route("/change-plan", methods=["GET", "POST"])
@login_required
def change_plan():
    if request.method == "POST":
        current_user.plan                = "free"
        current_user.subscription_id     = None
        current_user.subscription_status = None
        current_user.subscription_ends   = None
        db.session.commit()
        flash("You've been downgraded to the Free plan.", "info")
        return redirect(url_for("pricing"))
    return render_template("billing/change_plan.html")


@billing_bp.route("/webhook", methods=["POST"])
@require_webhook_secret
def webhook():
    payload = request.get_json(force=True) or {}
    current_app.logger.info(f"[Razorpay] Webhook received: {payload.get('event')}")
    parsed = parse_webhook(payload)
    dispatch_webhook(parsed, db)
    return "", 200