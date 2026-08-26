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

上表只是核心几张。`app/models/` 下实际有 **21** 张 `rp_ai_` 表,除上面 8 张之外还有:

```
rp_ai_skill_version           技能版本(发布历史)
rp_ai_skill_comment           评论
rp_ai_skill_download          下载记录
rp_ai_skill_audit_record      审计记录(事实,不可重算)
rp_ai_skill_finding           审计发现(命中项)
rp_ai_activity                活动流
rp_ai_login_event             登录事件
rp_ai_usage_event             用量事件
rp_ai_org_snapshot            组织树快照(部门用量报表用)
rp_ai_market_item             市场条目
rp_ai_market_item_version     市场条目版本
rp_ai_market_download         市场下载
rp_ai_market_favorite         市场收藏
```

**表名的 `rp_ai_` 前缀是硬要求**:每天的备份按这个前缀在运行时取表,不写死表名。新表不带这个前缀
就不会被备份,而且生产库里还有别的团队的 29 张表,前缀也是区分依据。

## 建表与加列

`create_all` **只建缺的表,不给已有表加列**。加列走 `app/migrate.py`:

```bash
python -m app.migrate     # 幂等,可重复跑
```

容器启动时 `docker-entrypoint.sh` 会先跑它,失败就不启动服务。所以模型加了字段之后,要在
`app/migrate.py` 里补上对应的 `ALTER TABLE`,否则线上那张表不会有这一列,而本地新建的库看不出问题。

## 本地管理员登录是个后门,默认口令没改

`app/config.py` 里:

```
local_admin_enabled   默认 True
local_admin_username  默认 adminmm
local_admin_password  默认 adminmm
```

它的用途是 AD 不可用时仍能进后台。**默认值等于用户名口令相同的公开后门**,生产上必须用环境变量
覆盖成强口令,或者把 `local_admin_enabled` 关掉。测试环境反过来依赖它 —— 那套环境没有配 AD 凭据,
只能用这个账号登录,所以关它之前先确认测试环境怎么登。

`ADMINMM` 同时是 `audit_admin_eid` 的默认值,即审计管理员的 EID。两者名字相近但是两回事:
一个是登录凭据,一个是权限判定用的 EID。

## 部门用量报表依赖一份组织树快照

用量报表要按部门汇总,部门树来自 MS Graph,取回后存成快照:

| 东西 | 在哪 |
|---|---|
| 快照文件 | `usage_report_org_snapshot`,默认 `/var/lib/rp-ai-portal/graph_tree_cache.json`,**不在 git 里** |
| 快照表 | `rp_ai_org_snapshot` |
| 组织树根 | `usage_report_org_root_id`(config.py 里的默认值) |
| 取回 | `tools/fetch_graph_tree.py` |
| 灌入 | `tools/push_org_snapshot.py` |

快照丢了,部门维度的报表就出不来。重建是跑 `fetch_graph_tree.py` 再 `push_org_snapshot.py`,
前提是有 MS Graph 的凭据 —— 那份凭据不在本仓的配置里,要向报表的需求方确认从哪拿。


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

## 本地跑测试碰不到真仓库,别把这道守卫拆了

`tests/conftest.py` 里有一个 autouse 固件:清空 `gitlab_token`、把技能仓项目名指成假值、
并换掉 `skill_content` 与 `gitlab_mr` 的 `_opener`。任何真实的出网调用都会让那条测试直接失败。

为什么必须有它:原来 conftest 不清 `gitlab_token`,而有几条测试没给 `gitlab_mr.review_suspect`
打桩。后果是谁的机器上配了真令牌,跑一次测试就往**生产**技能仓开三个真 MR。生产技能仓因此累积了
66 个垃圾 MR,已全部关闭。

两个实现细节不要改:

* 守卫放在 `_opener`(真正的出网口),不放在 `_fetch` / `_post` 上 —— 有两条测试**故意**验证
  `_fetch` 怎么处理 404 与 502,它们自己换掉 `_opener`。
* 用 `pytest.fail` 而不是 `assert`。`AssertionError` 会被 `review_suspect` 的 `except Exception`
  吃掉,守卫就失效了。

## 提交前扫一遍密钥

```bash
.venv/bin/python -m detect_secrets scan --baseline .secrets.baseline
```

`.secrets.baseline` 记着**已经看过、确认不是密钥**的那 8 处,所以这条命令只会报新出现的东西。
远端还有一道 pre-receive 扫描,但它目前只警告不拒绝 —— 一个没人处理的警告,就是下一次真泄露被放过
的方式,所以在本地先挡一层。

那 8 处逐行核对过,都是示例或测试夹具:`.gitlab-ci.yml` 里 CI 一次性数据库的 `ci:ci@localhost`、
`app/config.py` 注释里的 `user:pass@host` 格式示例、以及 6 处测试用的签名密钥与请求体里的
`"password": "pw"` 之类。**baseline 不会掩盖新的密钥**:验证过,往仓里放一个 AWS key 格式的字符串,
带 baseline 复扫照样报出来。

写测试用的假凭据时,把它拼出来而不是写成字面量(`"glpat-" + "…"`):否则扫描器会拦下这次提交,而它
拦的恰好是"验证凭据检测"的那个测试。

本仓不含 Dockerfile 与 compose:流水线来自共享模板,部署由环境描述决定。

| 我要做什么 | 去哪 |
|---|---|
| 发布、回滚、查现在跑的是什么、排查失败 | [platform-ci 操作手册](https://gitlab.internal.ericsson.com/rp-ai-miao/platform-ci/-/blob/main/README.md) |
| 改本服务的端口、环境变量、挂载目录 | [platform-ci `deploy/`](https://gitlab.internal.ericsson.com/rp-ai-miao/platform-ci/-/tree/main/deploy) |
| 查某个令牌配在哪、到期、失效了什么会坏 | [platform-ci CREDENTIALS.md](https://gitlab.internal.ericsson.com/rp-ai-miao/platform-ci/-/blob/main/CREDENTIALS.md) |
| 为什么这样分工、集群、备份恢复 | [platform-ci PLATFORM.md](https://gitlab.internal.ericsson.com/rp-ai-miao/platform-ci/-/blob/main/PLATFORM.md) |

合并到 `main` 后流水线产出镜像 `armdocker.rnd.ericsson.se/proj-rp-ps-ai/rp-ai-portal-be:<commit sha>`;
发布就是在环境描述里把镜像标签改成它。

镜像空间 2026-08-26 换成了我们自己的 `proj-rp-ps-ai`(原来的 `proj-rvc` 是 RVC 项目的空间)。
当前推镜像用的是个人账号而不是服务账号,细节与待办见 CREDENTIALS.md。

本仓产出**一个**镜像 `proj-rp-ps-ai/rp-ai-portal-be:<sha>`(对账器用同一个镜像,只换 entrypoint)。

审计能力已经拆到 `rp-ai-miao/skill-audit`,那个仓产出引擎 `skill-audit` 与判定器 `skill-audit-judge`。
本仓不再装第三方扫描器、也不再构建判定器;审计通过一次 HTTP 调用完成,客户端是
`app/services/audit_engine.py`。边界见 `.kiro/specs/skill-audit/service-split.md`。

判定器的 kiro-cli 登录态在命名卷里(`*_judge-kiro-home`、`*_judge-kiro-share`),环境描述把它们
声明为 `external` 并写死卷名。卷名一变判定器就是未登录状态,继而所有上传 fail-closed 转人工复核 ——
所以迁仓时镜像名逐字不变。

各环境端口:验收后端 9100、前端 9101;生产后端 9000、前端 9001。

本地跑测试用 `pytest`(判定器可在测试里替换)。要跑起整套服务用验收环境,不要在本仓再造一份
compose——那会与环境描述各说各话。

## 这个仓的设计文档

都在 `.kiro/specs/skill-audit/`,按"读了能干什么"排:

| 文档 | 讲什么 | 状态 |
|---|---|---|
| `repo-access.md` | 门户怎么读写技能内容:不持有可变副本,按不可变引用读,发布是带前置条件的原子写。删除的两个方向。实测过的接口行为 | 已实现并在生产验过。剩一步:收敛器补 manifest 扫描 |
| `service-split.md` | 审计为什么拆成独立服务、边界怎么画、失败语义 | 已实现,引擎在 `rp-ai-miao/skill-audit` |
| `security-compliance.md` | 安全合规维度:查什么、怎么设计、实测效果 | 已实现 |
| `data-compliance.md` | 数据合规维度 | **只有设计,一行没实现**;它依据的那份数据决定 2026 年 9 月到期 |
| `duplication.md` | 重复性(查重)维度 | 设计写在实现之前,实际实现见 !47 / 前端 !22 / skill-audit !1(尚未评审) |

## 还剩的活

| 事 | 说明 |
|---|---|
| 收敛器补 manifest 扫描 | 规格在 `repo-access.md` 末尾,前置条件(删除语义)已满足。同时要兜住"复核 MR 存在"这条投影的重算 |
| `instructions` 列的迁移删除 | 详情接口的正文改成按引用读之后,这一列**没有读者**了;写者还剩一个 `app/seed.py`(灌种子数据时写)。注意 `upload.py` 与 `skills.py` 里的 `instructions=` 是 `store()` 的入参,不是这一列。生产库里还有 22 行非空数据,删列前先确认没有别的消费方 |
| 那三个查重 MR 的评审 | !47(本仓,76 处改动)、前端 !22(10 处)、skill-audit !1(26 处),都还是 opened |
| 评审时注意探针脚本 | `feature/dup-portal-integration` 上有 `skill_audit/dup_probes/` 六十多个脚本和 jsonl 结果。**main 上目前是干净的**,评审时别让它们合进来 |

