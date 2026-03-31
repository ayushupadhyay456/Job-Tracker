"""
core/razorpay.py — Razorpay integration replacing Gumroad.

Install: pip install razorpay
.env keys needed:
    RAZORPAY_KEY_ID=rzp_live_xxxxx
    RAZORPAY_KEY_SECRET=xxxxxx
    RAZORPAY_WEBHOOK_SECRET=xxxxxx
    RAZORPAY_PLAN_ID_MONTHLY=plan_xxxxx
    RAZORPAY_PLAN_ID_BIANNUAL=plan_xxxxx
    RAZORPAY_PLAN_ID_ANNUAL=plan_xxxxx
    TRIAL_DAYS=3
"""

import os
import hmac
import hashlib
import razorpay
from datetime import datetime, timedelta, UTC
from functools import wraps
from flask import request, abort, current_app

# ── Client ────────────────────────────────────────────────────────────────────
client = razorpay.Client(
    auth=(
        os.getenv("RAZORPAY_KEY_ID"),
        os.getenv("RAZORPAY_KEY_SECRET"),
    )
)

# ── Plan IDs (set these in Razorpay dashboard → Subscriptions → Plans) ────────
PLAN_IDS = {
    "monthly":  os.getenv("RAZORPAY_PLAN_ID_MONTHLY"),
    "biannual": os.getenv("RAZORPAY_PLAN_ID_BIANNUAL"),
    "annual":   os.getenv("RAZORPAY_PLAN_ID_ANNUAL"),
}

# Exposed so billing.py can validate plan names
PLAN_URLS = PLAN_IDS   # kept same name for drop-in compatibility

# ── Trial period (days) ───────────────────────────────────────────────────────
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "3"))


# ── Create a Razorpay Subscription ───────────────────────────────────────────
def create_subscription(plan: str, user_email: str, user_name: str = "") -> dict:
    """
    Creates a Razorpay subscription and returns the subscription object.
    The front-end uses subscription['id'] to open Razorpay Checkout.

    Trial is implemented via `start_at` — a future Unix timestamp telling
    Razorpay when to begin the first billing cycle. The customer's card is
    saved at checkout but nothing is charged until that date.
    """
    plan_id = PLAN_IDS.get(plan)
    if not plan_id:
        raise ValueError(f"Unknown plan: {plan}")

    total_count = {
        "monthly":  12,   # renews up to 12 times (~1 year)
        "biannual": 4,    # renews up to 4 times (~2 years)
        "annual":   3,    # renews up to 3 times
    }.get(plan, 12)

    payload = {
        "plan_id":      plan_id,
        "total_count":  total_count,
        "quantity":     1,
        "notify_info": {
            "notify_phone": None,
            "notify_email": user_email,
        },
        "notes": {
            "name":  user_name,
            "email": user_email,
            "plan":  plan,
        },
    }

    # Trial via start_at — correct Razorpay approach (not trial_period field)
    if TRIAL_DAYS > 0:
        trial_end = datetime.now(UTC) + timedelta(days=TRIAL_DAYS)
        payload["start_at"] = int(trial_end.timestamp())

    subscription = client.subscription.create(payload)
    return subscription


# ── Kept for drop-in compatibility with app.py ────────────────────────────────
def get_checkout_url(plan: str, user_email: str, user_name: str = "") -> str:
    """
    Returns a local /billing/checkout/<plan> URL.
    The actual Razorpay subscription is created when that page loads.
    """
    return f"/billing/checkout/{plan}?email={user_email}&name={user_name}"


# ── Verify Razorpay payment signature ────────────────────────────────────────
def verify_payment_signature(razorpay_subscription_id: str,
                             razorpay_payment_id: str,
                             razorpay_signature: str) -> bool:
    """Call this after the JS checkout completes to confirm payment is real."""
    try:
        client.utility.verify_subscription_payment_signature({
            "razorpay_subscription_id": razorpay_subscription_id,
            "razorpay_payment_id":      razorpay_payment_id,
            "razorpay_signature":       razorpay_signature,
        })
        return True
    except razorpay.errors.SignatureVerificationError:
        return False


# ── Webhook signature verification decorator ─────────────────────────────────
def require_webhook_secret(f):
    """
    Decorator — verifies Razorpay webhook signature.
    Applied to your /billing/webhook route.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        secret    = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
        signature = request.headers.get("X-Razorpay-Signature", "")
        body      = request.get_data()

        expected = hmac.new(
            secret.encode(), body, hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(expected, signature):
            current_app.logger.warning("Razorpay webhook signature mismatch")
            abort(400)

        return f(*args, **kwargs)
    return decorated


# ── Parse incoming Razorpay webhook payload ───────────────────────────────────
def parse_webhook(payload: dict) -> dict:
    """
    Normalises a Razorpay webhook event into a flat dict
    that dispatch_webhook() can act on.
    """
    event   = payload.get("event", "")
    entity  = payload.get("payload", {})

    sub_obj = entity.get("subscription", {}).get("entity", {})
    pay_obj = entity.get("payment", {}).get("entity", {})

    notes   = sub_obj.get("notes") or pay_obj.get("notes") or {}
    email   = notes.get("email") or pay_obj.get("email", "")
    plan    = notes.get("plan", "")
    sub_id  = sub_obj.get("id") or pay_obj.get("subscription_id", "")

    return {
        "event":           event,
        "email":           email,
        "plan":            plan,
        "subscription_id": sub_id,
        "status":          sub_obj.get("status", ""),
        "cancelled": event in (
            "subscription.cancelled",
            "subscription.completed",
            "subscription.expired",
        ),
    }


# ── Dispatch webhook to update User record ───────────────────────────────────
def dispatch_webhook(parsed: dict, db) -> None:
    """
    Updates the User row based on the webhook event.
    Pass in the SQLAlchemy db instance.
    """
    from core.models import User

    email = parsed.get("email", "").lower()
    event = parsed.get("event", "")

    if not email:
        current_app.logger.warning("[Razorpay] Webhook missing email, skipping")
        return

    user = User.query.filter_by(email=email).first()
    if not user:
        current_app.logger.warning(f"[Razorpay] No user found for {email}")
        return

    if event in ("subscription.activated", "subscription.charged", "payment.captured"):
        user.plan                = parsed.get("plan") or user.plan or "monthly"
        user.subscription_id     = parsed.get("subscription_id")
        user.subscription_status = "active"

    elif event == "subscription.halted":
        # Trial ended, card declined on first charge
        user.subscription_status = "payment_failed"

    elif parsed.get("cancelled"):
        user.subscription_status = "cancelled"
        user.plan                = "free"
        user.subscription_id     = None

    db.session.commit()
    current_app.logger.info(f"[Razorpay] {event} → user {email} updated")