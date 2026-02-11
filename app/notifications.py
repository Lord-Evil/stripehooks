"""Notification services - Telegram and Email."""
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import httpx
from typing import Optional

from .database import get_setting


async def verify_telegram_bot(token: str) -> tuple[bool, str | dict]:
    """
    Verify token via getMe. Returns (ok, bot_info dict) on success or (False, error_message) on failure.
    bot_info has: username, first_name, link (e.g. https://t.me/NoHumanBot)
    """
    url = f"https://api.telegram.org/bot{token.strip()}/getMe"
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, timeout=10.0)
            data = r.json()
            if data.get("ok") and data.get("result"):
                result = data["result"]
                username = result.get("username", "")
                first_name = result.get("first_name", "Bot")
                link = f"https://t.me/{username}" if username else ""
                return True, {"username": username, "first_name": first_name, "link": link}
            return False, data.get("description", "Invalid response from Telegram")
    except Exception as e:
        return False, str(e)


async def send_telegram_message(chat_id: str, text: str) -> tuple[bool, str]:
    """Send a message via Telegram Bot API."""
    token = await get_setting("telegram_bot_token")
    if not token:
        return False, "Telegram bot token not configured"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}

    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(url, json=payload, timeout=10.0)
            data = r.json()
            if data.get("ok"):
                return True, "Sent"
            return False, data.get("description", "Unknown error")
    except Exception as e:
        return False, str(e)


async def send_email(to_email: str, subject: str, body: str) -> tuple[bool, str]:
    """Send an email via configured SMTP."""
    host = await get_setting("smtp_host")
    port_str = await get_setting("smtp_port")
    security = await get_setting("smtp_security") or "starttls"
    user = await get_setting("smtp_user")
    password = await get_setting("smtp_password")
    from_email = await get_setting("smtp_from_email")

    if not host:
        return False, "SMTP not configured"

    default_port = {"ssl": 465, "starttls": 587, "none": 25}.get(security, 587)
    port = int(port_str) if port_str else default_port
    from_addr = from_email or user or "noreply@localhost"

    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        if security == "ssl":
            with smtplib.SMTP_SSL(host, port) as server:
                if user and password:
                    server.login(user, password)
                server.sendmail(from_addr, to_email, msg.as_string())
        else:
            with smtplib.SMTP(host, port) as server:
                if security == "starttls":
                    server.starttls()
                if user and password:
                    server.login(user, password)
                server.sendmail(from_addr, to_email, msg.as_string())
        return True, "Sent"
    except Exception as e:
        return False, str(e)
