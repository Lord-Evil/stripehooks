"""Stripe webhook handler."""
import json
import logging
from datetime import datetime
from typing import Any, Optional

import stripe
from fastapi import Request, HTTPException, BackgroundTasks

from .database import get_setting, get_rules_for_product, insert_payment_history
from .notifications import send_telegram_message, send_email

logger = logging.getLogger(__name__)


def _format_timestamp(ts: int) -> str:
    """Format Unix timestamp as human-readable date/time."""
    try:
        return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError):
        return str(ts)


async def _get_product_name(product_id: str) -> str:
    """Fetch product name from Stripe API. Returns product_id if fetch fails."""
    api_key = await get_setting("stripe_api_key")
    if not api_key:
        return product_id
    try:
        stripe.api_key = api_key
        product = stripe.Product.retrieve(product_id)
        return product.get("name") or product_id
    except Exception as e:
        logger.debug("Could not fetch product name: %s", e)
        return product_id


async def _get_customer_info(customer_id: str) -> tuple[str, str]:
    """Fetch customer name and email. Returns (name, email)."""
    api_key = await get_setting("stripe_api_key")
    if not api_key or not customer_id:
        return ("", "")
    try:
        stripe.api_key = api_key
        customer = stripe.Customer.retrieve(customer_id)
        name = customer.get("name") or ""
        email = customer.get("email") or ""
        return (name, email)
    except Exception as e:
        logger.debug("Could not fetch customer: %s", e)
        return ("", "")


def get_nested(data: dict, path: str) -> Optional[Any]:
    """Get a value from nested dict using dot notation, e.g. 'data.object.payment_details.order_reference'."""
    keys = path.split(".")
    current = data
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return None
    return current


def extract_product_id(event_data: dict) -> Optional[str]:
    """Extract product ID from webhook event. Tries multiple possible paths."""
    paths = [
        "data.object.payment_details.order_reference",
        "data.object.metadata.product_id",
        "data.object.metadata.order_reference",
    ]
    for path in paths:
        val = get_nested(event_data, path)
        if val:
            return str(val)
    return None


async def process_payment_succeeded(event: dict) -> None:
    """Process payment_intent.succeeded event and execute configured actions."""
    product_id = extract_product_id(event)
    if not product_id:
        logger.warning("Could not extract product ID from event: %s", json.dumps(event)[:500])
        return

    rules = await get_rules_for_product(product_id)
    if not rules:
        logger.info("No rules configured for product %s, skipping", product_id)
        return

    payment_intent = event.get("data", {}).get("object", {})
    amount = payment_intent.get("amount", 0) / 100  # cents to units
    currency = payment_intent.get("currency", "usd").upper()
    pi_id = payment_intent.get("id", "unknown")
    created = payment_intent.get("created")
    payment_datetime = _format_timestamp(created) if created else "N/A"

    # Client name: charges.data[].billing_details, shipping.name, metadata, or customer
    client_name = ""
    client_email = ""
    charges_data = get_nested(payment_intent, "charges.data") or []
    if isinstance(charges_data, list) and charges_data:
        # Use first charge's billing_details (Payment Links typically put it here)
        first_charge = charges_data[0] if isinstance(charges_data[0], dict) else {}
        billing = first_charge.get("billing_details") or {}
        client_name = billing.get("name") or ""
        client_email = billing.get("email") or ""
    if not client_name:
        client_name = (
            get_nested(payment_intent, "shipping.name")
            or payment_intent.get("metadata", {}).get("customer_name")
            or payment_intent.get("metadata", {}).get("name")
            or ""
        )
    if not client_email:
        client_email = (
            payment_intent.get("receipt_email")
            or payment_intent.get("metadata", {}).get("customer_email")
            or payment_intent.get("metadata", {}).get("email")
            or ""
        )
    # Fetch from Customer if we have customer ID and missing name/email
    customer_id = payment_intent.get("customer")
    if isinstance(customer_id, str):
        cust_name, cust_email = await _get_customer_info(customer_id)
        if not client_name:
            client_name = cust_name
        if not client_email:
            client_email = cust_email

    product_name = await _get_product_name(product_id)

    # Record in history for analytics (amount in smallest units)
    amount_raw = payment_intent.get("amount", 0)
    await insert_payment_history(
        product_id=product_id,
        product_name=product_name,
        amount=amount_raw,
        currency=payment_intent.get("currency", "usd"),
        payment_intent_id=pi_id,
        created_at=created or 0,
    )

    lines = []
    if client_name:
        lines.append(f"Client name: {client_name}")
    if client_email:
        lines.append(f"Client email: {client_email}")
    product_line = f"Product: {product_name}" + (f" ({product_id})" if product_name != product_id else "")
    lines.append(product_line)
    lines.append(f"Amount: {amount} {currency}")
    lines.append(f"Payment date: {payment_datetime}")
    lines.append(f"Payment Intent: {pi_id}")

    message = "Payment received!\n\n" + "\n".join(lines)

    email_subject = f"Payment received for {product_name}"
    email_body = message

    for rule in rules:
        action_type = rule["type"]
        action_value = rule["value"]

        if action_type == "telegram":
            ok, err = await send_telegram_message(action_value, message)
            if not ok:
                logger.error("Telegram send failed: %s", err)
        elif action_type == "email":
            ok, err = await send_email(action_value, email_subject, email_body)
            if not ok:
                logger.error("Email send failed: %s", err)
        else:
            logger.warning("Unknown action type: %s", action_type)


async def handle_stripe_webhook(request: Request, background_tasks: BackgroundTasks) -> dict:
    """Handle incoming Stripe webhook, verify signature, and process events."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    webhook_secret = await get_setting("webhook_secret")
    if not webhook_secret:
        raise HTTPException(status_code=500, detail="Webhook secret not configured")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid payload: {e}")
    except stripe.SignatureVerificationError as e:
        raise HTTPException(status_code=400, detail=f"Invalid signature: {e}")

    if event["type"] == "payment_intent.succeeded":
        background_tasks.add_task(process_payment_succeeded, event)
    else:
        logger.info("Unhandled event type: %s", event["type"])

    # Fix: run notification sends directly in process_payment_succeeded
    # instead of adding as separate background tasks
    return {"received": True}
