# Argus Agent

Server-side Amtrak price monitoring agent that runs on Railway. Scrapes Amtrak prices using Playwright and sends web push notifications to the Argus Chrome extension when fares drop.

## How It Works

1. **The Chrome extension registers a watch** by calling `POST /register` with the user's route, pinned trains, and browser push subscription

2. **Argus polls prices every 90 seconds** using a headless Chromium browser (Playwright with stealth evasions) — no Amtrak tab needs to be open

3. **When a price drops**, Argus sends a web push notification to the user's browser:
   - Notification title: `Train 651 dropped to $54`
   - Notification body: `NYP→PHL · Coach · Book now!`
   - Click action: opens the Amtrak booking page

4. **All prices are stored** in SQLite for history tracking and drop detection

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/register` | Register a watch from the Chrome extension |
| `POST` | `/subscribe` | Store a browser push subscription |
| `GET` | `/vapid-public-key` | Get the public VAPID key for browser subscription |
| `GET` | `/health` | Health check — returns active watch count |

### POST /register

Register or update a price watch.

```json
{
  "route": {
    "origin": "NYP",
    "destination": "PHL",
    "date": "04/07/2026"
  },
  "trains": [
    { "trainNumber": "651" },
    { "trainNumber": "657" }
  ],
  "subscription": {
    "endpoint": "https://fcm.googleapis.com/fcm/send/...",
    "keys": {
      "p256dh": "...",
      "auth": "..."
    }
  }
}
```

- `route` (required): origin/destination station codes and travel date
- `trains` (optional): pinned trains to watch — if empty, watches all trains
- `subscription` (optional): browser push subscription object

### POST /subscribe

Store a web push subscription independently.

```json
{
  "subscription": {
    "endpoint": "https://fcm.googleapis.com/fcm/send/...",
    "keys": { "p256dh": "...", "auth": "..." }
  },
  "watch_id": 123
}
```

### GET /vapid-public-key

Returns the public VAPID key for the browser to create a push subscription.

```json
{ "publicKey": "BEl62i..." }
```

## Setup

### Prerequisites

- Python 3.11+
- An [Anthropic API key](https://console.anthropic.com/)
- VAPID keys for web push (generate with `npx web-push generate-vapid-keys`)

### Install

```bash
cd argus-agent
pip install -r requirements.txt
playwright install chromium
```

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes | Claude API key for natural language parsing |
| `VAPID_PRIVATE_KEY` | Yes | Private VAPID key for web push |
| `VAPID_PUBLIC_KEY` | Yes | Public VAPID key (shared with browser) |
| `VAPID_CLAIMS_EMAIL` | No | Contact email for VAPID claims (default: `kieranbarr7@gmail.com`) |
| `FLASK_PORT` | No | Server port (default: `5000`) |

#### Generate VAPID Keys

```bash
npx web-push generate-vapid-keys
```

This outputs a public/private key pair. Set them as environment variables.

### Run Locally

```bash
export ANTHROPIC_API_KEY="sk-ant-xxxxx"
export VAPID_PRIVATE_KEY="your-private-key"
export VAPID_PUBLIC_KEY="your-public-key"

python main.py
```

This starts:
- Flask server on port 5000
- Background price poller (checks every 90s with jitter)

### Deploy to Railway

The repo includes `Procfile` and `railway.json` for Railway deployment.

1. Push to GitHub
2. Connect the repo in [Railway](https://railway.app/)
3. Set the environment variables in Railway's dashboard
4. Railway auto-deploys on push

## Architecture

```
argus-agent/
├── main.py           — Flask server + APScheduler entry point
├── bot.py            — Web push notification sender (pywebpush)
├── parser.py         — Claude-powered natural language parser
├── monitor.py        — Playwright price scraper + polling logic
├── recommender.py    — Price recommendations + alert formatting
├── db.py             — SQLite (watches, price history, push subscriptions)
├── config.py         — Environment-based configuration
├── requirements.txt  — Python dependencies
├── Procfile          — Railway/Heroku process definition
└── railway.json      — Railway deployment config
```

## How Price Monitoring Works

- Prices are checked every 90 seconds (with random jitter to avoid detection)
- Uses a headless Chromium browser with stealth evasions
- Navigates directly to Amtrak's pre-filled results URL
- Captures fare data from Amtrak's API responses during page load
- Falls back to DOM extraction if API interception fails
- Compares current prices to previous observations — alerts on drops
- If specific trains are pinned, only those trains trigger alerts
- All prices are stored in SQLite for history tracking
