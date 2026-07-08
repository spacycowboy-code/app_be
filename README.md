# RP AI Portal — Backend

FastAPI + SQLAlchemy backend for the RP AI Portal. Provides REST APIs for
portal users, the skill marketplace, favorites (collections) and reviews,
backed by MySQL.

## Tech stack

- FastAPI + Uvicorn
- SQLAlchemy 2.x (ORM) + PyMySQL driver
- Pydantic v2 (camelCase API, snake_case models)
- MySQL 8

## Data model

| Table                 | Purpose                                                   |
| --------------------- | --------------------------------------------------------- |
| `rp_ai_portal`        | Portal users, keyed by `eid`                              |
| `rp_ai_skill`         | Skills (marketplace items), with `uploader_eid`           |
| `rp_ai_skill_case`    | Example cases for a skill (child of `rp_ai_skill`)        |
| `rp_ai_sub_skill`     | Sub-skills of a skill (child)                             |
| `rp_ai_input_param`   | Input parameters of a skill (child)                       |
| `rp_ai_category`      | Skill categories (slug id)                                |
| `rp_ai_user_favorite` | User ↔ skill collections (favorites)                      |
| `rp_ai_skill_review`  | Multi-dimensional reviews + aggregate rating source       |

A user's **uploaded** skills are `rp_ai_skill` rows where `uploader_eid`
matches their EID; their **collected** skills live in `rp_ai_user_favorite`.

## Setup

Requires Python 3.11+.

```bash
python3 -m venv .venv
. .venv/bin/activate

# Internal PyPI proxy (bypass the HTTP proxy for the ericsson.se domain):
NO_PROXY=.ericsson.se pip install -r requirements.txt \
  --index-url https://arm.sero.gic.ericsson.se/artifactory/api/pypi/pypi-remote/simple \
  --trusted-host arm.sero.gic.ericsson.se
```

Copy `.env.example` to `.env` and set `DATABASE_URL`.

## Create tables

```bash
python -m app.init_db
```

## Run

```bash
./start.sh                 # or: uvicorn app.main:app --reload
```

- API base: `http://localhost:8000`
- Interactive docs: `http://localhost:8000/docs`
- Health check: `http://localhost:8000/health`

## API overview

| Method | Path                                   | Description                     |
| ------ | -------------------------------------- | ------------------------------- |
| POST   | `/api/users`                           | Create/get user (by EID)        |
| GET    | `/api/users/{eid}`                     | Get user                        |
| GET    | `/api/users/{eid}/skills`              | Skills uploaded by the user     |
| GET    | `/api/users/{eid}/favorites`           | User's collected skills         |
| POST   | `/api/users/{eid}/favorites`           | Collect a skill                 |
| DELETE | `/api/users/{eid}/favorites/{skillId}` | Remove a collection             |
| GET    | `/api/skills`                          | List/filter skills              |
| POST   | `/api/skills`                          | Create a skill (with children)  |
| GET    | `/api/skills/{id}`                     | Skill detail                    |
| PUT    | `/api/skills/{id}`                     | Update a skill                  |
| DELETE | `/api/skills/{id}`                     | Delete a skill                  |
| GET    | `/api/skills/{id}/reviews`             | List reviews                    |
| POST   | `/api/skills/{id}/reviews`             | Add review (recomputes rating)  |
| GET    | `/api/categories`                      | List categories                 |
| POST   | `/api/categories`                      | Create a category               |

`GET /api/skills` supports query params: `categoryId`, `status`, `q`
(name/description search), `trending`.

The API speaks **camelCase** JSON to match the Angular frontend.

## Deployment (Docker + GitLab CI)

The service ships as a Docker image and is deployed by the GitLab pipeline.

### Files

| File                 | Purpose                                                        |
| -------------------- | -------------------------------------------------------------- |
| `Dockerfile`         | `python:3.11-slim` + `git`/`ssh`, installs deps, runs uvicorn  |
| `.dockerignore`      | Keeps `.venv`, `.git`, `.env`, caches out of the build context |
| `docker-compose.yml` | Runs the container: port, volumes, `.env`, health check        |
| `log_conf.yaml`      | uvicorn log config: console + rotating files under `/app/logs` |
| `.gitlab-ci.yml`     | Pipeline: `lint` → `test` → `deploy`                           |

### Runtime layout

- **Port**: host `9000` → container `8000` (uvicorn).
- **Volumes** (host ↔ container):
  - `/proj/rp_ait_prod/services/ai-portal-be/gitlab` ↔ `/proj/radio_ivc_rvc_workspace/ezhgmio/rp-ai-skill` (skill repo, = `SKILL_REPO_PATH`)
  - `/proj/rp_ait_prod/logs/ai-portal-be` ↔ `/app/logs` (`backend.log`, `access.log`; 10 MB × 5 rotation)
- Logs also go to stdout, so `docker compose logs -f` still works.

### Configuration: the `PROD_ENV` variable

The repo has **no `.env`** (it is gitignored). Production configuration lives
in a single **File-type** GitLab CI/CD variable so all settings — current and
future — are managed in one place, outside the repo.

Add it under **Settings → CI/CD → Variables**:

- **Type**: `File`
- **Key**: `PROD_ENV`
- **Value**: the complete production `.env` content
- **Protected**: yes (and mark `main` as a protected branch)

The `deploy` job writes it out with `cp "$PROD_ENV" .env` before starting the
container. Example content:

```dotenv
DATABASE_URL=mysql+pymysql://<user>:<password>@<host>:<port>/<db>?charset=utf8mb4
APP_NAME=RP AI Portal Backend
DEBUG=false
CORS_ORIGINS=https://<frontend-domain>
GITLAB_TOKEN=<token>
GITLAB_AGENTS_GROUP=rp-ai/rp-ai-agent
GITLAB_TOOLS_GROUP=rp-ai/rp-ai-tool
SKILL_REPO_PUSH=false
```

Settings with sensible defaults in `app/config.py` (e.g. `SKILL_REPO_PATH`,
`GIT_AUTHOR_*`, `MAX_UPLOAD_BYTES`, `GITLAB_URL`) can be omitted.
`SKILL_REPO_PUSH=true` requires an SSH key mounted into the container
(uncomment the `.ssh` volume in `docker-compose.yml`).

### Deploy access (SSH)

The GitLab runner is a docker executor with no host Docker daemon, so it cannot
run a persistent container itself. Instead the `deploy` job **SSHes into the
deploy server** and runs `docker compose` there (the server builds locally — no
registry push). Add these File/Variable CI/CD variables (mark Protected):

| Variable         | Type     | Purpose                                             |
| ---------------- | -------- | --------------------------------------------------- |
| `DEPLOY_HOST`    | Variable | deploy server hostname/IP                           |
| `DEPLOY_USER`    | Variable | SSH login user (able to run docker on the server)   |
| `DEPLOY_DIR`     | Variable | target dir, e.g. `/proj/rp_ait_prod/services/ai-portal-be` |
| `DEPLOY_PORT`    | Variable | optional SSH port (defaults to 22)                  |
| `DEPLOY_SSH_KEY` | File     | private key; its public key is in the server's `authorized_keys` |
| `PROD_ENV`       | File     | full production `.env` (see above)                  |

Server prerequisites: Docker + `docker compose` v2 + `rsync` installed,
`DEPLOY_USER` can run docker, and the bind-mount paths in `docker-compose.yml`
exist (Docker creates them on first run).

### Pipeline stages

- **lint** — `ruff check` (non-blocking for now).
- **test** — install deps, smoke-import `app.main`, run `pytest` (tolerates
  "no tests collected").
- **deploy** — SSHes into the deploy server, `rsync`s the code + `.env`, then
  runs `docker compose up -d --build` there. Gated to **`main` + manual**:
  after a merge to `main`, open the pipeline and click ▶ on the `deploy` job.

### Deploy flow

1. Work on a feature branch → push runs `lint` + `test`.
2. Open a Merge Request into `main` (protect `main` so changes only land via MR).
3. After merge, the `main` pipeline exposes a manual `deploy` job — click ▶.
4. The job rsyncs the repo to `$DEPLOY_DIR`, writes `.env` from `PROD_ENV`, and
   runs `docker compose up -d --build` on the server.

### Requirements / notes

- The `deploy` job needs network + SSH access from the runner to `DEPLOY_HOST`;
  the deploy server needs Docker + compose. (If instead you get a runner that
  runs on the deploy host with host-docker access, the job could run
  `docker compose` directly and drop the SSH hop.)
- The API currently authenticates only via the `X-User-Eid` header (no real
  auth) — do not expose port 9000 to the public internet; keep it internal or
  behind a gateway.
- Manual local run: `docker compose up -d --build` (needs a local `.env`).
