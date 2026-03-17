#!/usr/bin/env python3
"""Run the StripeHooks server."""
import uvicorn

from app.config import HOST, PORT

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=HOST,
        port=PORT,
        reload=True,
        access_log=False,  # Use RequestLoggingMiddleware for richer logs (X-Forwarded-For, User-Agent)
    )
