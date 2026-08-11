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

## 部署(镜像 + 环境描述)

本仓不再包含 Dockerfile(判定器那份除外)、compose 与 deploy 任务。原因见
`rp-ai-miao/platform-ci` 的 `DESIGN.md`:构建过程属于平台,产物是不可变镜像,
"在哪台机器上跑哪个标签"属于环境。

### 流水线产出

合并到 `main` 后,共享模板会构建并推送两个镜像:

| 镜像 | 内容 |
|---|---|
| `proj-rvc/rp-ai/rp-ai-portal-be:<sha>` | 后端;对账器用同一个镜像,只换 entrypoint |
| `proj-rvc/rp-ai/skill-audit-judge:<sha>` | 判定器;镜像里装了 kiro-cli,配方是本仓的 `Dockerfile.judge` |

本仓在 `.gitlab-ci.yml` 里声明的只有:两个镜像名、应用路径、额外 apt 包、pipx 装的扫描器、
启动前跑迁移的 entrypoint,以及测试期用的假 `DATABASE_URL`。

### 发布与回滚

在 `platform-ci` 改对应环境描述里的镜像标签:

| 环境 | 文件 | 端口 |
|---|---|---|
| 验收 | `deploy/staging-audit/compose.yml` | 后端 9100,前端 9101 |
| 生产 | `deploy/prod/compose.yml` | 后端 9000,前端 9001 |

改标签并合并后触发对应的手动部署任务。**回滚 = revert 那次提交再触发一次**,不需要重新构建。

密钥也在环境侧:`AUDIT_PROD_ENV` / `AUDIT_STAGING_ENV`(File 型 CI 变量),由部署任务投放到
环境目录,不进 git。

### 判定器的登录态

判定器的 kiro-cli 登录存在命名卷里(`*_judge-kiro-home` / `*_judge-kiro-share`),环境描述
把它们声明为 `external` 并写死卷名。换目录或改卷名会让判定器变成未登录,继而所有上传
fail-closed 转人工复核。

### 本地开发

`pytest` 覆盖判定逻辑(判定器可在测试里替换)。需要跑起整套时用验收环境,不要在本仓再造
一份 compose——那会变成与环境描述竞争的第二份事实。
