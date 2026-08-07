# RP AI Portal backend (FastAPI + uvicorn), Python 3.11.
FROM python:3.11-slim

# git   : required — skill uploads are stored via local git commits/tags.
# ssh   : only needed if pushing the skill repo to GitLab over SSH.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Cisco AI Defense skill-scanner (static analyzers, no key) is a CLI tool we
# shell out to — install it ISOLATED via pipx so its heavy/pinned deps don't
# conflict with the web stack (e.g. python-multipart). The `skill-scanner`
# binary lands on PATH for the audit pipeline's subprocess call.
ENV PATH="/root/.local/bin:${PATH}"
RUN pip install --no-cache-dir pipx \
    && pipx install cisco-ai-skill-scanner==2.0.12 \
    && skill-scanner --version

# Application source.
COPY app ./app
# Red-line rules consumed by the audit pipeline at runtime.
COPY skill_audit/rules.md ./skill_audit/rules.md
COPY log_conf.yaml .

# Logs are written here; the host mounts this to a unified path.
RUN mkdir -p /app/logs

# Migrate-then-serve entrypoint (idempotent schema migration on start).
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

# Build-time commit SHA -> runtime env, surfaced by /api/version. Placed late so
# a new commit doesn't invalidate the dependency install layers above.
ARG GIT_COMMIT=dev
ENV APP_GIT_COMMIT=$GIT_COMMIT

# uvicorn listens on 8000 inside the container (mapped to the host in compose).
EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
