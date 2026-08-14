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
| POST   | `/api/auth/login`                      | LDAP login → JWT session        |
| GET    | `/api/auth/me`                         | Identity of the current session  |
| GET    | `/api/auth/verify`                     | 204 + `X-Auth-Eid`, for nginx `auth_request` |
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

`GET /api/auth/verify` exists for the frontend nginx, not for the browser: it
reduces the session check to a status code plus an `X-Auth-Eid` header, so nginx
can hand that signum to a proxied tool that knows nothing about portal JWTs (the
EriDoc tool under `/api/eridoc/`, which authenticates by `X-Forwarded-User`).
`/api/auth/me` cannot serve that purpose — `auth_request` can only read a header,
not a JSON body.

The API speaks **camelCase** JSON to match the Angular frontend.

## 构建与部署

本仓不含 Dockerfile 与 compose:流水线来自共享模板,部署由环境描述决定。

| 我要做什么 | 去哪 |
|---|---|
| 发布、回滚、查现在跑的是什么、排查失败 | [platform-ci 操作手册](https://gitlab.internal.ericsson.com/rp-ai-miao/platform-ci/-/blob/main/README.md) |
| 改本服务的端口、环境变量、挂载目录 | [platform-ci `deploy/`](https://gitlab.internal.ericsson.com/rp-ai-miao/platform-ci/-/tree/main/deploy) |
| 为什么这样分工 | [platform-ci DESIGN.md](https://gitlab.internal.ericsson.com/rp-ai-miao/platform-ci/-/blob/main/DESIGN.md) |

合并到 `main` 后流水线产出镜像 `armdocker.rnd.ericsson.se/proj-rvc/rp-ai/rp-ai-portal-be:<commit sha>`;
发布就是在环境描述里把镜像标签改成它。

本仓产出**两个**镜像:

| 镜像 | 内容 |
|---|---|
| `proj-rvc/rp-ai/rp-ai-portal-be:<sha>` | 后端;对账器用同一个镜像,只换 entrypoint |
| `proj-rvc/rp-ai/skill-audit-judge:<sha>` | 判定器;镜像里装了 kiro-cli,配方是本仓的 `Dockerfile.judge` |

判定器的 kiro-cli 登录态在命名卷里(`*_judge-kiro-home`、`*_judge-kiro-share`),环境描述把它们
声明为 `external` 并写死卷名。卷名一变判定器就是未登录状态,继而所有上传 fail-closed 转人工复核。

各环境端口:验收后端 9100、前端 9101;生产后端 9000、前端 9001。

本地跑测试用 `pytest`(判定器可在测试里替换)。要跑起整套服务用验收环境,不要在本仓再造一份
compose——那会与环境描述各说各话。

