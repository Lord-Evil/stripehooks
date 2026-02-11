"""Application configuration."""
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root (directory containing app/)
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Database path - use STRIPEHOOKS_DB_PATH for Docker (e.g. /app/data/stripehooks.db)
DB_PATH = Path(os.environ.get("STRIPEHOOKS_DB_PATH", str(_PROJECT_ROOT / "stripehooks.db")))

# Admin password - set via env or default for dev
ADMIN_PASSWORD = os.environ.get("STRIPEHOOKS_ADMIN_PASSWORD", "admin")
# Base URL for webhook - must be publicly accessible
BASE_URL = os.environ.get("STRIPEHOOKS_BASE_URL", "http://localhost:8000")
# Session secret - set via env for production
SESSION_SECRET = os.environ.get("STRIPEHOOKS_SESSION_SECRET", secrets.token_hex(32))
# Server host and port
HOST = os.environ.get("STRIPEHOOKS_HOST", "0.0.0.0")
PORT = int(os.environ.get("STRIPEHOOKS_PORT", "8000"))
