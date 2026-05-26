#!/bin/sh
# Entrypoint: runs the scheduler daemon in the background and Streamlit in
# the foreground. Both share /data for SQLite and the upstream memory log.
#
# PYTHONPATH=/app is required because Streamlit launches the dashboard via
# direct-script invocation, which sets sys.path[0] to the script's directory
# (/app/portfolio/dashboard/) — not /app. Without PYTHONPATH, "from portfolio.state
# import store" raises ModuleNotFoundError.

set -eu

export PYTHONPATH=/app

mkdir -p /data/memory /data/cache /data/logs

echo "[start] launching scheduler daemon..."
python -m portfolio.scheduler.runner > /data/logs/scheduler.log 2>&1 &

echo "[start] launching FastAPI dashboard on :8080..."
exec uvicorn portfolio.dashboard.server:app \
    --host 0.0.0.0 \
    --port 8080 \
    --workers 1 \
    --log-level info
