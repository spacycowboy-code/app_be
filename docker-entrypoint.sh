#!/bin/sh
# Backend container entrypoint: wait for the database, apply the idempotent
# schema migration, then serve. The wait matters when the database is a sibling
# container that is still starting up.
set -e
echo "[entrypoint] applying schema migration..."
i=1
while [ "$i" -le 12 ]; do
  if python -m app.migrate; then
    echo "[entrypoint] migration applied"
    break
  fi
  echo "[entrypoint] database not ready yet (attempt $i), retrying in 5s..."
  i=$((i + 1))
  sleep 5
done
[ "$i" -gt 12 ] && echo "[entrypoint] WARN: migration never succeeded; serving anyway"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --log-config log_conf.yaml
