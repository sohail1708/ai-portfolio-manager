#!/bin/sh
# Entrypoint: runs the scheduler daemon in the background and Streamlit in
# the foreground. Both share /data for SQLite and the upstream memory log.

set -eu

mkdir -p /data/memory /data/cache /data/logs

echo "[start] launching scheduler daemon..."
python -m portfolio.scheduler.runner > /data/logs/scheduler.log 2>&1 &

echo "[start] launching Streamlit dashboard on :8080..."
exec streamlit run portfolio/dashboard/app.py \
    --server.port=8080 \
    --server.address=0.0.0.0 \
    --server.headless=true \
    --browser.gatherUsageStats=false
