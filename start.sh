#!/usr/bin/env bash
# Start the RP AI Portal backend (FastAPI + uvicorn).
set -e
cd "$(dirname "$0")"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

exec .venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT" "$@"
