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

# Application source.
COPY app ./app
COPY log_conf.yaml .

# Logs are written here; the host mounts this to a unified path.
RUN mkdir -p /app/logs

# Build-time commit SHA -> runtime env, surfaced by /api/version. Placed late so
# a new commit doesn't invalidate the dependency install layers above.
ARG GIT_COMMIT=dev
ENV APP_GIT_COMMIT=$GIT_COMMIT

# uvicorn listens on 8000 inside the container (mapped to 9000 on the host).
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-config", "log_conf.yaml"]
