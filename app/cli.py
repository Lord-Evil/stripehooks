#!/usr/bin/env python3
"""CLI for StripeHooks admin tasks."""
import argparse
import asyncio
import getpass
import hashlib
import os
import re
import sys

from .config import SESSION_SECRET
from .database import get_setting, set_setting, init_db


def _hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode()).hexdigest()


def _validate_password(password: str) -> tuple[bool, str]:
    """Validate password strength. Returns (ok, error_message)."""
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


async def _reset_password(password: str) -> None:
    await init_db()
    salt = await get_setting("admin_password_salt") or SESSION_SECRET
    await set_setting("admin_password_salt", salt)
    await set_setting("admin_password_hash", _hash_password(password, salt))


def main():
    parser = argparse.ArgumentParser(description="StripeHooks CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    reset_parser = subparsers.add_parser("reset-password", help="Reset admin password")
    reset_parser.add_argument(
        "-p", "--password",
        help="New password (or set STRIPEHOOKS_NEW_PASSWORD env). If omitted, prompts.",
    )

    args = parser.parse_args()

    if args.command == "reset-password":
        password = args.password or os.environ.get("STRIPEHOOKS_NEW_PASSWORD")
        if not password:
            password = getpass.getpass("New password: ")
            password2 = getpass.getpass("Confirm: ")
            if password != password2:
                print("Passwords do not match.", file=sys.stderr)
                sys.exit(1)
        password = password.strip()
        ok, err = _validate_password(password)
        if not ok:
            print(f"Invalid password: {err}", file=sys.stderr)
            sys.exit(1)
        asyncio.run(_reset_password(password))
        print("Admin password reset successfully.")


if __name__ == "__main__":
    main()
