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

# Install Python deps first (cached layer).
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv pip install --system .

# Now copy source. /app is owned by root but we run as a non-root user.
COPY . .

RUN useradd --create-home --uid 1000 appuser \
    && chmod +x deploy/start.sh

USER appuser

EXPOSE 8080

# tini → start.sh → (scheduler in bg) + (streamlit in fg)
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["./deploy/start.sh"]
