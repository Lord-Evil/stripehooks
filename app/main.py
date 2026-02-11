"""FastAPI application - Stripe webhook handler with admin UI."""
import hashlib
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request, Depends, HTTPException, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from .config import BASE_URL, SESSION_SECRET

BASE_DIR = Path(__file__).resolve().parent
from .database import init_db, get_setting, set_setting, get_product_rules, add_product_rule, delete_product_rule, set_rule_enabled, get_all_product_rules, get_payment_analytics
from .notifications import verify_telegram_bot, send_email
from .webhook import handle_stripe_webhook
import stripe

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def verify_admin(request: Request) -> bool:
    """Verify admin session."""
    return request.session.get("admin") == True


def _hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode()).hexdigest()


def _validate_admin_password(password: str) -> tuple[bool, str]:
    """
    Validate admin password strength. Returns (ok, error_message).
    Requirements: min 16 chars, uppercase, lowercase, digit, special char.
    """
    if len(password) < 16:
        return False, "Password must be at least 16 characters"
    if not re.search(r"[A-Z]", password):
        return False, "Password must contain at least one uppercase letter"
    if not re.search(r"[a-z]", password):
        return False, "Password must contain at least one lowercase letter"
    if not re.search(r"\d", password):
        return False, "Password must contain at least one digit"
    if not re.search(r"[!@#$%^&*()_+\-=\[\]{};':\"\\|,.<>/?]", password):
        return False, "Password must contain at least one special character"
    return True, ""


async def _verify_admin_password(password: str) -> bool:
    """Verify password against stored hash."""
    stored = await get_setting("admin_password_hash")
    if not stored:
        return False
    salt = await get_setting("admin_password_salt") or SESSION_SECRET
    return _hash_password(password, salt) == stored


async def _ensure_admin_password() -> None:
    """On first launch: set admin password from env if not in DB."""
    if await get_setting("admin_password_hash") is not None:
        return
    from .config import ADMIN_PASSWORD
    salt = SESSION_SECRET
    await set_setting("admin_password_salt", salt)
    await set_setting("admin_password_hash", _hash_password(ADMIN_PASSWORD, salt))


async def _get_nav_context() -> dict:
    """Context for nav (stripe/webhook configured)."""
    stripe_configured = await get_setting("stripe_api_key") is not None
    webhook_configured = await get_setting("webhook_secret") is not None
    return {
        "stripe_configured": stripe_configured,
        "webhook_configured": webhook_configured,
        "stripe_ready": stripe_configured and webhook_configured,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await _ensure_admin_password()
    yield


app = FastAPI(title="StripeHooks", lifespan=lifespan)

from starlette.middleware.sessions import SessionMiddleware

app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Redirect to admin or login."""
    if verify_admin(request):
        return RedirectResponse(url="/admin", status_code=302)
    return RedirectResponse(url="/admin/login", status_code=302)


@app.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/admin/login")
async def login(request: Request, password: str = Form(...)):
    if await _verify_admin_password(password):
        request.session["admin"] = True
        return RedirectResponse(url="/admin", status_code=302)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Invalid password"},
        status_code=401,
    )


@app.get("/admin/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/admin/login", status_code=302)


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    if not verify_admin(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    nav = await _get_nav_context()
    smtp_configured = await get_setting("smtp_host") is not None
    telegram_configured = await get_setting("telegram_bot_token") is not None

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            **nav,
            "smtp_configured": smtp_configured,
            "telegram_configured": telegram_configured,
            "base_url": BASE_URL,
        },
    )


@app.get("/admin/stripe", response_class=HTMLResponse)
async def admin_stripe(request: Request):
    if not verify_admin(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    nav = await _get_nav_context()
    return templates.TemplateResponse(
        "stripe_config.html",
        {
            "request": request,
            "base_url": BASE_URL,
            **nav,
        },
    )


def _normalize_stripe_key(key: str | None) -> str | None:
    """Remove whitespace and stray chars that can break auth (e.g. from copy-paste)."""
    if not key:
        return None
    key = key.strip()
    if key.endswith(":") and ":" not in key[:-1]:
        key = key[:-1]  # Stray colon from curl -u format
    return key or None


@app.post("/admin/stripe/api-key")
async def save_stripe_api_key(request: Request, api_key: str = Form(...)):
    if not verify_admin(request):
        raise HTTPException(status_code=401)
    normalized = _normalize_stripe_key(api_key)
    if not normalized:
        return RedirectResponse(
            url="/admin/stripe?error=Invalid+API+key",
            status_code=302,
        )
    await set_setting("stripe_api_key", normalized)
    return RedirectResponse(url="/admin/stripe", status_code=302)


@app.post("/admin/stripe/webhook")
async def create_webhook(request: Request):
    if not verify_admin(request):
        raise HTTPException(status_code=401)

    api_key = _normalize_stripe_key(await get_setting("stripe_api_key"))
    if not api_key:
        return RedirectResponse(
            url="/admin/stripe?error=Configure+Stripe+API+key+first",
            status_code=302,
        )

    webhook_url = f"{BASE_URL.rstrip('/')}/webhook/stripe"

    try:
        # List existing webhooks for this URL (pass api_key explicitly to avoid global state)
        endpoints = stripe.WebhookEndpoint.list(limit=100, api_key=api_key)
        existing = next((e for e in endpoints.data if e.url == webhook_url), None)

        if existing:
            stripe.WebhookEndpoint.modify(
                existing.id,
                enabled_events=["payment_intent.succeeded"],
                api_key=api_key,
            )
            # Keep existing secret - Stripe only returns it on create
            secret = await get_setting("webhook_secret")
            if not secret:
                # Shouldn't happen if we had a webhook, but fallback
                raise ValueError("Webhook exists but no secret stored - delete webhook in Stripe Dashboard and recreate")
        else:
            endpoint = stripe.WebhookEndpoint.create(
                url=webhook_url,
                enabled_events=["payment_intent.succeeded"],
                description="StripeHooks payment_intent.succeeded",
                api_key=api_key,
            )
            secret = endpoint.get("secret")

        if secret:
            await set_setting("webhook_secret", secret)
    except stripe.StripeError as e:
        logger.exception("Stripe webhook creation failed")
        from urllib.parse import quote
        msg = "Authentication+error" if "Invalid API Key" in str(e) else quote(str(e), safe="")
        return RedirectResponse(
            url=f"/admin/stripe?error={msg}",
            status_code=302,
        )

    return RedirectResponse(url="/admin/stripe?success=Webhook+configured", status_code=302)


@app.get("/admin/products", response_class=HTMLResponse)
async def admin_products(request: Request):
    if not verify_admin(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    nav = await _get_nav_context()
    if not nav["stripe_ready"]:
        return RedirectResponse(url="/admin", status_code=302)

    api_key = _normalize_stripe_key(await get_setting("stripe_api_key"))
    products = []
    if api_key:
        try:
            prod_list = stripe.Product.list(active=True, limit=100, api_key=api_key)
            products = [{"id": p.id, "name": p.name, "description": p.description or ""} for p in prod_list.data]
        except stripe.StripeError as e:
            products = []
            logger.warning("Failed to fetch products: %s", e)

    rules = await get_all_product_rules()
    product_names = {p["id"]: p["name"] for p in products}

    return templates.TemplateResponse(
        "products.html",
        {"request": request, "products": products, "rules": rules, "product_names": product_names, **nav},
    )


@app.post("/admin/products/rule")
async def add_rule(
    request: Request,
    product_id: str = Form(...),
    action_type: str = Form(...),
    action_value: str = Form(...),
):
    if not verify_admin(request):
        raise HTTPException(status_code=401)
    if action_type not in ("email", "telegram"):
        raise HTTPException(status_code=400, detail="Invalid action type")
    action_value = action_value.strip()
    if not action_value:
        raise HTTPException(status_code=400, detail="Action value required")
    await add_product_rule(product_id, action_type, action_value)
    return RedirectResponse(url="/admin/products", status_code=302)


def _get_date_range(
    preset: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[int | None, int | None]:
    """Return (start_ts, end_ts) for preset. None means no limit."""
    now = datetime.now(timezone.utc)
    if preset == "custom" and start_date and end_date:
        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(
                hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc
            )
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, microsecond=999999, tzinfo=timezone.utc
            )
            end_dt = min(end_dt, now)
            return (int(start_dt.timestamp()), int(end_dt.timestamp()))
        except ValueError:
            pass
    if preset == "all_time":
        return (None, None)
    if preset == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return (int(start.timestamp()), int(now.timestamp()))
    if preset == "yesterday":
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1) - timedelta(seconds=1)
        return (int(start.timestamp()), int(end.timestamp()))
    if preset == "this_week":
        start = now - timedelta(days=now.weekday())
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        return (int(start.timestamp()), int(now.timestamp()))
    if preset == "last_week":
        start = now - timedelta(days=now.weekday() + 7)
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7) - timedelta(seconds=1)
        return (int(start.timestamp()), int(end.timestamp()))
    if preset == "this_month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return (int(start.timestamp()), int(now.timestamp()))
    if preset == "last_month":
        first_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start = (first_this - timedelta(days=1)).replace(day=1)
        end = first_this - timedelta(seconds=1)
        return (int(start.timestamp()), int(end.timestamp()))
    if preset == "this_year":
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return (int(start.timestamp()), int(now.timestamp()))
    if preset == "last_year":
        start = now.replace(year=now.year - 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = now.replace(year=now.year - 1, month=12, day=31, hour=23, minute=59, second=59, microsecond=999999)
        return (int(start.timestamp()), int(end.timestamp()))
    return (None, None)


def _get_date_range_strings(
    preset: str, start_date: str | None, end_date: str | None
) -> tuple[str, str]:
    """Return (start_date_str, end_date_str) for the datepicker for any preset."""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if preset == "custom":
        if start_date and end_date:
            return (start_date, end_date)
        return (today, today)
    if preset == "all_time":
        # Default to last 30 days for display when "all time" is selected
        start = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        return (start, today)
    if preset == "today":
        return (today, today)
    if preset == "yesterday":
        y = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        return (y, y)
    if preset == "this_week":
        start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        return (start, today)
    if preset == "last_week":
        start = now - timedelta(days=now.weekday() + 7)
        start_str = start.strftime("%Y-%m-%d")
        end_str = (start + timedelta(days=6)).strftime("%Y-%m-%d")
        return (start_str, end_str)
    if preset == "this_month":
        start = now.replace(day=1).strftime("%Y-%m-%d")
        return (start, today)
    if preset == "last_month":
        first_this = now.replace(day=1)
        start = (first_this - timedelta(days=1)).replace(day=1)
        end = first_this - timedelta(days=1)
        return (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    if preset == "this_year":
        start = now.replace(month=1, day=1).strftime("%Y-%m-%d")
        return (start, today)
    if preset == "last_year":
        start = now.replace(year=now.year - 1, month=1, day=1)
        end = now.replace(year=now.year - 1, month=12, day=31)
        return (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    return (today, today)


@app.get("/admin/history", response_class=HTMLResponse)
async def admin_history(request: Request):
    if not verify_admin(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    nav = await _get_nav_context()
    if not nav["stripe_ready"]:
        return RedirectResponse(url="/admin", status_code=302)

    preset = request.query_params.get("range", "all_time")
    start_param = request.query_params.get("start")
    end_param = request.query_params.get("end")
    start_ts, end_ts = _get_date_range(preset, start_param, end_param)
    start_date_str, end_date_str = _get_date_range_strings(preset, start_param, end_param)

    analytics = await get_payment_analytics(start_ts=start_ts, end_ts=end_ts)

    # Format amounts for display (cents to units)
    for row in analytics:
        row["total_display"] = row["total_amount"] / 100
        row["currency_upper"] = (row["currency"] or "usd").upper()

    product_names = {}
    api_key = _normalize_stripe_key(await get_setting("stripe_api_key"))
    if api_key:
        try:
            prod_list = stripe.Product.list(active=True, limit=100, api_key=api_key)
            product_names = {p.id: p.name for p in prod_list.data}
        except stripe.StripeError:
            pass

    for row in analytics:
        if not row["product_name"] or row["product_name"] == row["product_id"]:
            row["product_name"] = product_names.get(row["product_id"], row["product_id"])

    presets = [
        ("all_time", "All time"),
        ("today", "Today"),
        ("yesterday", "Yesterday"),
        ("this_week", "This week"),
        ("last_week", "Last week"),
        ("this_month", "This month"),
        ("last_month", "Last month"),
        ("this_year", "This year"),
        ("last_year", "Last year"),
        ("custom", "Custom"),
    ]
    preset_label = next((label for val, label in presets if val == preset), "All time")

    response = templates.TemplateResponse(
        "history.html",
        {
            "request": request,
            "analytics": analytics,
            "preset": preset,
            "preset_label": preset_label,
            "presets": presets,
            "start_date": start_date_str,
            "end_date": end_date_str,
            **nav,
        },
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


@app.post("/admin/products/rule/{rule_id}/delete")
async def delete_rule(request: Request, rule_id: int):
    if not verify_admin(request):
        raise HTTPException(status_code=401)
    await delete_product_rule(rule_id)
    return RedirectResponse(url="/admin/products", status_code=302)


@app.post("/admin/products/rule/{rule_id}/toggle")
async def toggle_rule(request: Request, rule_id: int, enabled: str = Form("")):
    if not verify_admin(request):
        raise HTTPException(status_code=401)
    new_state = enabled.lower() not in ("0", "false", "no")
    await set_rule_enabled(rule_id, new_state)
    return RedirectResponse(url="/admin/products", status_code=302)


@app.get("/admin/smtp", response_class=HTMLResponse)
async def admin_smtp(request: Request):
    if not verify_admin(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    nav = await _get_nav_context()
    security = await get_setting("smtp_security") or "starttls"
    return templates.TemplateResponse(
        "smtp_config.html",
        {
            "request": request,
            "smtp_host": await get_setting("smtp_host") or "",
            "smtp_port": await get_setting("smtp_port") or "587",
            "smtp_security": security,
            "smtp_user": await get_setting("smtp_user") or "",
            "smtp_from_email": await get_setting("smtp_from_email") or "",
            **nav,
        },
    )


@app.post("/admin/smtp")
async def save_smtp(
    request: Request,
    smtp_host: str = Form(...),
    smtp_port: str = Form("587"),
    smtp_security: str = Form("starttls"),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    smtp_from_email: str = Form(""),
):
    if not verify_admin(request):
        raise HTTPException(status_code=401)
    if smtp_security not in ("none", "starttls", "ssl"):
        smtp_security = "starttls"
    await set_setting("smtp_host", smtp_host.strip())
    default_port = {"ssl": "465", "starttls": "587", "none": "25"}.get(smtp_security, "587")
    await set_setting("smtp_port", smtp_port.strip() or default_port)
    await set_setting("smtp_security", smtp_security)
    await set_setting("smtp_user", smtp_user.strip())
    if smtp_password:
        await set_setting("smtp_password", smtp_password)
    await set_setting("smtp_from_email", smtp_from_email.strip())
    return RedirectResponse(url="/admin/smtp?success=1", status_code=302)


@app.post("/admin/smtp/test")
async def test_smtp(request: Request, test_email: str = Form(...)):
    if not verify_admin(request):
        raise HTTPException(status_code=401)
    test_email = test_email.strip()
    if not test_email:
        return RedirectResponse(url="/admin/smtp?error=" + _url_quote("Email required"), status_code=302)
    ok, err = await send_email(
        test_email,
        "StripeHooks SMTP Test",
        "This is a test email from StripeHooks. Your SMTP settings are working correctly.",
    )
    if ok:
        return RedirectResponse(url="/admin/smtp?test_success=1", status_code=302)
    return RedirectResponse(url=f"/admin/smtp?test_error={_url_quote(err)}", status_code=302)


def _url_quote(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


@app.get("/admin/telegram", response_class=HTMLResponse)
async def admin_telegram(request: Request):
    if not verify_admin(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    nav = await _get_nav_context()
    token = await get_setting("telegram_bot_token")
    bot_info = None
    bot_error = None
    if token:
        cached = await get_setting("telegram_bot_info")
        if cached:
            try:
                bot_info = json.loads(cached)
            except (json.JSONDecodeError, TypeError):
                pass
        if not bot_info:
            ok, result = await verify_telegram_bot(token)
            if ok:
                bot_info = result
                await set_setting("telegram_bot_info", json.dumps(bot_info))
            else:
                bot_error = result

    return templates.TemplateResponse(
        "telegram_config.html",
        {
            "request": request,
            "has_token": bool(token),
            "bot_info": bot_info,
            "bot_error": bot_error,
            **nav,
        },
    )


@app.post("/admin/telegram")
async def save_telegram(request: Request, bot_token: str = Form("")):
    if not verify_admin(request):
        raise HTTPException(status_code=401)

    token = bot_token.strip()
    if not token:
        return RedirectResponse(url="/admin/telegram", status_code=302)

    ok, result = await verify_telegram_bot(token)
    if not ok:
        from urllib.parse import quote
        return RedirectResponse(
            url=f"/admin/telegram?error={quote(str(result), safe='')}",
            status_code=302,
        )

    await set_setting("telegram_bot_token", token)
    await set_setting("telegram_bot_info", json.dumps(result))
    return RedirectResponse(url="/admin/telegram?success=1", status_code=302)


@app.get("/admin/account", response_class=HTMLResponse)
async def admin_account(request: Request):
    if not verify_admin(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    nav = await _get_nav_context()
    return templates.TemplateResponse(
        "account.html",
        {"request": request, **nav},
    )


@app.post("/admin/account/password")
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
):
    if not verify_admin(request):
        raise HTTPException(status_code=401)

    if not await _verify_admin_password(current_password):
        return RedirectResponse(
            url="/admin/account?error=" + _url_quote("Current+password+incorrect"),
            status_code=302,
        )

    new_password = new_password.strip()
    ok, err = _validate_admin_password(new_password)
    if not ok:
        return RedirectResponse(
            url="/admin/account?error=" + _url_quote(err.replace(" ", "+")),
            status_code=302,
        )

    salt = await get_setting("admin_password_salt") or SESSION_SECRET
    await set_setting("admin_password_hash", _hash_password(new_password, salt))

    return RedirectResponse(url="/admin/account?success=1", status_code=302)


@app.post("/webhook/stripe")
async def stripe_webhook(request: Request, background_tasks: BackgroundTasks):
    return await handle_stripe_webhook(request, background_tasks)
