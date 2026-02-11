# StripeHooks

A FastAPI web app that handles Stripe webhooks (`payment_intent.succeeded`) and sends notifications via Telegram and email.

## Features

- **Stripe webhook** for `payment_intent.succeeded` events
- **Product-based rules**: configure actions per product ID (from `data.object.payment_details.order_reference` or `data.object.metadata.product_id`)
- **Actions**: send email, send Telegram message (multiple per product)
- **Admin UI**: single-user web interface for configuration
- **Sales History**: analytics with date range presets (today, yesterday, this week, etc.) and custom datepicker
- **SQLite** storage for settings and rules

## Setup

```bash
pip install -r requirements.txt
```

## Configuration

Environment variables can be set directly or via a `.env` file in the project root. Copy `.env.example` to `.env` and edit as needed.

| Variable | Description |
|----------|-------------|
| `STRIPEHOOKS_ADMIN_PASSWORD` | Initial admin password on first launch (default: `admin`). Stored in DB; change via Settings → Admin. |
| `STRIPEHOOKS_BASE_URL` | Public URL for webhook (e.g. `https://yourdomain.com`) |
| `STRIPEHOOKS_SESSION_SECRET` | Session secret (optional, auto-generated if not set) |
| `STRIPEHOOKS_DB_PATH` | SQLite database path (default: `./stripehooks.db`). Use `/app/data/stripehooks.db` in Docker. |
| `STRIPEHOOKS_HOST` | Server host (default: `0.0.0.0`) |
| `STRIPEHOOKS_PORT` | Server port (default: `8000`) |

## Run

```bash
python run.py
# or
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 and log in with the admin password.

## Docker

Build and run with Docker (Alpine-based image):

```bash
docker build -t stripehooks .
docker run -d -p 8000:8000 -v stripehooks_data:/app/data stripehooks
```

Persist the database by mounting a volume at `/app/data`. Pass env vars as needed:

```bash
docker run -d -p 8000:8000 -v stripehooks_data:/app/data \
  -e STRIPEHOOKS_ADMIN_PASSWORD=your-secure-password \
  -e STRIPEHOOKS_BASE_URL=https://your-domain.com \
  stripehooks
```

## Admin UI (Steps)

1. **Stripe**: Enter your Stripe secret key (sk_). Click "Setup Webhook" to create a webhook for `payment_intent.succeeded`. Ensure `STRIPEHOOKS_BASE_URL` is your public URL.
2. **SMTP**: Configure mail server (host, port, user, password, from email).
3. **Telegram**: Enter your bot token from @BotFather.
4. **Products**: Add rules: product ID + action (email or telegram) + target (email address or Telegram chat ID).
5. **History**: View sales analytics by product; filter by date range (presets or custom picker).

## Product ID Extraction

The app extracts the product ID from the webhook payload in this order:

1. `data.object.payment_details.order_reference`
2. `data.object.metadata.product_id`
3. `data.object.metadata.order_reference`

Ensure your PaymentIntent includes the product ID in one of these locations when creating payments.

## Local Testing

Use [Stripe CLI](https://stripe.com/docs/webhooks/test#cli) to forward webhooks to your local server:

```bash
stripe listen --forward-to localhost:8000/webhook/stripe
```
