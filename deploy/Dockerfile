# Image for the AI Portfolio Manager on Fly.io.
# Runs BOTH the daily scheduler (APScheduler) and the Streamlit dashboard
# inside a single machine that mounts a persistent volume at /data for SQLite
# and the upstream memory log.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORTFOLIO_DB_PATH=/data/portfolio_state.db \
    TRADINGAGENTS_MEMORY_LOG_PATH=/data/memory/trading_memory.md \
    TRADINGAGENTS_CACHE_DIR=/data/cache \
    TRADINGAGENTS_RESULTS_DIR=/data/logs

# System deps: tini for clean signal handling.
RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends tini ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install uv, then sync deps from the LOCK FILE (not pyproject.toml) so we
# get the exact versions tested locally. Critical: yfinance broke API in 1.x;
# the lock file pins to 0.2.x which stockstats still understands.
RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
# --frozen: refuse to update the lock; fail if there's drift.
# --no-dev: skip dev-only groups (pytest etc).
# --no-install-project: don't install the project itself yet (we copy source after).
RUN uv sync --frozen --no-dev --no-install-project

# Now copy source and install the project on top of the synced env.
COPY . .
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

RUN useradd --create-home --uid 1000 appuser \
    && chmod +x deploy/start.sh

USER appuser

EXPOSE 8080

# tini → start.sh → (scheduler in bg) + (streamlit in fg)
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["./deploy/start.sh"]
