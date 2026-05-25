# Deploying to Fly.io

One always-on Fly machine runs both the Streamlit dashboard (port 8080,
exposed publicly via HTTPS) and the APScheduler-driven daily run loop.
A 1 GB persistent volume mounted at `/data` holds the SQLite state, the
upstream reflection memory log, and the yfinance cache, so nothing is
lost across deploys.

## Prereqs

- A Fly.io account (https://fly.io/app/sign-up)
- `flyctl` installed:
  ```bash
  brew install flyctl
  ```
- Logged in:
  ```bash
  fly auth login
  ```

## One-time setup

From the **repo root** (not from `deploy/`):

```bash
# 1. Launch the app (pick a unique name when prompted, e.g. "sohail-ai-portfolio")
fly launch \
    --config fly.toml \
    --dockerfile deploy/Dockerfile \
    --no-deploy

# 2. Create the persistent volume (1 GB is way more than we need).
fly volumes create portfolio_data \
    --size 1 \
    --region iad \
    --yes

# 3. Set secrets. These DON'T appear in fly.toml; they're encrypted at rest.
fly secrets set \
    OPENAI_API_KEY="sk-proj-..." \
    ALPACA_API_KEY="PK..." \
    ALPACA_SECRET_KEY="..." \
    ALPACA_BASE_URL="https://paper-api.alpaca.markets" \
    PORTFOLIO_UNIVERSE="AAPL,MSFT,NVDA,GOOGL,AMZN,META,TSLA,AVGO,ORCL" \
    PORTFOLIO_STARTING_CASH="100000" \
    PORTFOLIO_CASH_PARK_TICKER="QQQ" \
    PORTFOLIO_RUN_CRON="0 16 * * 1-5"

# 4. Deploy.
fly deploy --config fly.toml
```

After deploy completes you'll get a URL like:
```
https://sohail-ai-portfolio.fly.dev
```
That's the dashboard. Visit it — Overview tab should load immediately
(no data yet), Memory tab will be empty until the first scheduled run.

## Redeploying after code changes

```bash
fly deploy --config fly.toml
```
That's it. State on `/data` is preserved.

## Watching logs

```bash
fly logs                                     # tail combined logs
fly ssh console -C "tail -f /data/logs/scheduler.log"  # scheduler-only
```

## Inspecting the SQLite DB

```bash
fly ssh console -C "sqlite3 /data/portfolio_state.db '.tables'"
fly ssh console -C "sqlite3 /data/portfolio_state.db 'SELECT * FROM nav_history ORDER BY snapshot_date DESC LIMIT 10'"
```

## Cost expectations

Free Fly tier ($5/mo credit) covers:
- 1× shared-cpu-1x, 512 MB RAM machine running 24/7
- 1 GB persistent volume
- Public HTTPS + custom domain support

Expected actual spend: **$0–3/mo**. The LLM cost (OpenAI) is the dominant
spend item, not infra.

## Stopping it

```bash
fly apps destroy <app-name>           # full teardown
# or:
fly scale count 0                     # pause but keep config + volume
```

## Notes / gotchas

- **DON'T put `.env` in the Docker image.** It's already in `.dockerignore`
  via `.gitignore`. Secrets go through `fly secrets set` only.
- **Volume is region-pinned.** If you ever change `primary_region` in
  `fly.toml`, you'll need to re-create the volume.
- **Single-machine setup.** Both processes run in one VM. If we ever want
  to scale the dashboard separately from the scheduler, we'd need to
  move to LiteFS or an external SQLite-replacement.
- **Cron in container time.** APScheduler's CronTrigger uses `America/New_York`
  (hard-coded in `portfolio/scheduler/runner.py`). Doesn't matter what the
  Fly host TZ is — the trigger does its own conversion.
