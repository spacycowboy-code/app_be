#!/bin/sh
# Backend container entrypoint: apply idempotent schema migration, then serve.
set -e
echo "[entrypoint] applying schema migration..."
python -m app.migrate || echo "[entrypoint] WARN: migrate failed; continuing"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --log-config log_conf.yaml
