#!/usr/bin/env bash
# Starts both vendor mocks and the gateway. Ctrl-C stops all three.
cd "$(dirname "$0")"
trap 'kill 0' EXIT
uv run uvicorn mocks.stripely:app --port 4001 --log-level warning &
uv run uvicorn mocks.adyenta:app --port 4002 --log-level warning &
uv run uvicorn gateway.main:app --port 8000 --log-level warning &
echo "stripely :4001  adyenta :4002  gateway :8000  ->  http://localhost:8000"
wait
