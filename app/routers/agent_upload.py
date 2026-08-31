"""包上传(tool / agent):包进 GitLab,条目进市场,立刻可见。

和 skill 上传的关系:形状一样(先体检、再发布),存储不一样。skill 全部落在一个 git 仓的
子目录里;这里每个包一个 GitLab 项目。这样做的直接好处是门户已有的下载与安装令牌两条路径
一行都不用改 —— 它们认 `gl-<项目id>`,而上传出来的条目正好就是这个形状。

**两种类型的上传路径完全一样,`itemType` 只决定两件事:** 市场那一行的 `item_type`,以及包
落到哪个 GitLab 组 —— agent 进 `rp-ai/rp-ai-user-agents`,tool 进 `rp-ai/rp-ai-user-tools`。
两个组各有自己的组访问令牌(组访问令牌只能访问它所属的那一个组),所以"哪个组"和"哪个凭据"
是同一个决定,一起装在 `gitlab_projects.UploadTarget` 里。

**收哪些格式:** zip 和 tar(`.tar`/`.tar.gz`/`.tgz`/`.tar.bz2`/`.tar.xz`),两种类型相同。
判断"这是什么"看**内容**不看扩展名,扩展名校验只用来告诉上传者该传什么。

三条规矩,和这里的每个决定都有关:

**这条路不跑安全审计。** 原来跑,但对包它从来不改流向 —— 产出只有一份给审核人看的报告,而
**唯一的闸门是批准的人**。于是它是纯成本:上传要多等几十秒,还多一个必须活着的下游服务。
所以审核页必须给审核人足够的事实(文件清单、可执行文件、包会做什么),否则批准就是盖章。
skill 那条路不受影响,它的审计**就是**闸门(`skill_audit.gate`),不是报告。

**两件事仍然拦,而它们不是审计。** 解不开的包(zip slip、符号链接)是我们自己会写到目录外面,
必须退回;带明文凭据的包 GitLab 的 pre-receive 钩子必定拒收,不提前说的结果不是"放过",
而是两分钟后给用户一句 GitLab 原文报错、组里还留下一个空项目。

**上传者确认的启动命令是权威值。** 存进数据库(提示词读它)和包里的 portal-agent.yaml(给
拿着包离开门户的人)。README 一个字都不改:改了下一版会被上传者自己的包覆盖。
"""
from __future__ import annotations

import json
import logging
import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from fastapi import Depends

from app.config import get_settings
from app.database import get_db
from app.models.category import RpAiCategory
from app.models.market import RpAiMarketItem
from app.models.market_version import RpAiMarketItemVersion
from app.models.user import RpAiPortal
from app.services import agent_package as pkg
from app.services import gitlab_projects as glw
from app.services.dept_groups import PSEUDO_CATEGORY_IDS, group_for_eid
from app.services.eids import acting_eid
from app.services.skill_storage import bump_minor

log = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/api/market/agent-upload", tags=["agent-upload"])

#: 每种条目类型收哪些打包格式。两种都收 zip 和 tar —— 一张表在这里,是为了让"收什么"
#: 有一个可以指的地方,而不是散在校验分支里。
#:
#: 判断格式看**内容**不看扩展名(`pkg.archive_kind`),所以一个改名成 `.zip` 的 tar 走的仍然
#: 是 tar 那条解包路径,不会报"这不是有效的 zip"。
ALLOWED_KINDS: dict[str, tuple[str, ...]] = {
    "tool": ("zip", "tar"),
    "agent": ("zip", "tar"),
}

#: 上传界面与错误提示里用的扩展名。真正的判断按内容,这里只是为了能对上传者说出"该传什么"。
ALLOWED_SUFFIXES: dict[str, tuple[str, ...]] = {
    "tool": pkg.ZIP_SUFFIXES + pkg.TAR_SUFFIXES,
    "agent": pkg.ZIP_SUFFIXES + pkg.TAR_SUFFIXES,
}


# --- 共用 ------------------------------------------------------------------


def _item_type(raw: str | None) -> str:
    """收哪两种类型的上传。默认 agent —— 这个端点原来只有 agent,老的调用方不带这个字段。

    `ai-platform` 不在其中:那类条目是一个别人运营的服务,它没有包可传,给它一个文件选择框
    只会让人以为门户能托管它。
    """
    value = (raw or "agent").strip().lower()
    if value not in ALLOWED_KINDS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"itemType 只能是 {'、'.join(ALLOWED_KINDS)}",
        )
    return value


async def _read_package(file: UploadFile | None, item_type: str) -> bytes:
    """读上传的包并按类型校验扩展名与大小。

    扩展名校验是给人看的提示,不是安全边界 —— 真正判断格式的是内容(`pkg.archive_kind`,
    由 `extract_safely` 按 `ALLOWED_KINDS` 强制)。两道都要:只看内容的话,一个传错文件的人
    得到的是"这不是有效的包",而他更需要知道的是"这里收 .zip / .tar.gz"。
    """
    suffixes = ALLOWED_SUFFIXES[item_type]
    wanted = " / ".join(suffixes)
    if file is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"请选择一个包({wanted})")
    name = (file.filename or "").lower()
    if not name.endswith(suffixes):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"包的格式只能是 {wanted}"
        )
    data = await file.read()
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"包超过 {settings.max_upload_bytes // (1024 * 1024)} MB 上限",
        )
    return data


def _unpack(
    data: bytes, item_type: str
) -> tuple[Path, list[pkg.FileEntry], pkg.Inspection]:
    """安全解包并体检。调用方负责删掉返回的临时目录。"""
    tmp = Path(tempfile.mkdtemp(prefix="agent-pkg-"))
    try:
        entries = pkg.extract_safely(
            data,
            tmp / "src",
            max_bytes=settings.max_upload_bytes,
            allow=ALLOWED_KINDS[item_type],
        )
    except pkg.PackageRejected as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.detail) from exc
    return tmp, entries, pkg.inspect(tmp / "src", entries)


def _secret_stop(root: Path, entries: list[pkg.FileEntry]) -> None:
    """带明文凭据就此为止 —— 不是我们的裁决,是 GitLab 一定会拒。"""
    hits = pkg.scan_secrets(root, entries)
    if not hits:
        return
    where = "、".join(f"{h['file']}:{h['line']}" for h in hits[:3])
    raise HTTPException(
        status.HTTP_400_BAD_REQUEST,
        f"包里有明文凭据({where}),GitLab 不会接收这个包。请删掉凭据、改用环境变量或"
        f"配置文件后重新上传。共命中 {len(hits)} 处。",
    )


def _inspection_out(ins: pkg.Inspection, *, artifact_name: str | None) -> dict:
    """确认屏与审核页共用的一份事实。两处渲染同一个结构,免得两边讲得不一样。"""
    d = ins.declared
    launch = d.launch
    return {
        "artifactName": artifact_name,
        "totalBytes": ins.total_bytes,
        "fileCount": len(ins.files),
        "topDir": ins.top_dir,
        "readmePath": ins.readme_path,
        "readmeText": ins.readme_text,
        "files": [{"path": f.path, "size": f.size} for f in ins.files[:500]],
        "filesTruncated": len(ins.files) > 500,
        "executables": ins.executables[:100],
        "envFiles": ins.env_files,
        "hasGitDir": ins.has_git_dir,
        "declared": {
            "launch": launch,
            "runtime": d.runtime,
            "keep": d.keep,
            "prerequisites": d.prerequisites,
            "sources": d.sources,
        },
        # 装依赖那几步,从包里实际有什么推出来。空表示推不出来 —— 提示词退回原来那句
        # "按 README 安装",那是诚实的兜底,不是失败。
        "install": [
            {"command": s.command, "note": s.note} for s in ins.install_steps
        ],
        "launchCandidates": ins.launch_candidates,
        # 每条候选的来源和"错了看不看得出来"。确认屏靠 certain 决定要不要把它当成已核对的
        # 值来显示 —— 结构化推导出来的可以,从 README 猜的必须显式标成待核对。
        "launchGuesses": [
            {"command": g.command, "source": g.source, "certain": g.certain}
            for g in ins.launch_guesses
        ],
        # 结构化来源命中了没有。留这一项是为了在决定"要不要上大模型兜底"之前先有数据:
        # 如果绝大多数包都能确定推导出来,那个兜底就不值得建。
        "launchCertain": bool(ins.launch_guesses and ins.launch_guesses[0].certain),
        # 唯一的自动可用性检查:启动命令提到的文件在不在包里。找不到不代表错(可能是装出来
        # 的 entry point),所以只是提示。
        "launchTargetFound": pkg.launch_target_exists(launch, ins.files) if launch else None,
    }


# --- 体检 ------------------------------------------------------------------


@router.post("/inspect")
async def inspect_package(
    item_type: str = Form("agent", alias="itemType"),
    file: UploadFile | None = File(None),
) -> dict:
    """解包体检,什么都不存。

    **这里不跑安全审计。** 原来跑,而且这个端点做成事件流就是为了它:审计要几十秒到几分钟,
    一条固定动画会先跑完、留下一个满格进度条和什么都没发生。但那次审计对 tool / agent 的包
    从来不改流向(`blocking: False`),产出只有一份给审核人看的报告 —— 而包的闸门是人工审核。
    所以它是纯成本:每次上传多等几十秒,还多一个必须活着的下游服务(引擎连不上时它返回的是
    "suspect,没能查",于是界面还得专门解释"这不等于通过")。

    去掉之后这一步只剩解包、扫明文凭据、把启动命令和安装步骤推出来 —— 全是本地的、毫秒级,
    所以事件流也一并去掉,直接一次答完。skill 那条路不受影响:它的审计**就是**闸门
    (见 `skill_audit.gate`),不是报告。

    `eid` / `name` 两个表单字段跟着审计一起没了用处(它们是审计的入参),所以这里不再声明 ——
    前端多送的字段 FastAPI 会忽略,老调用方不会因此失败。
    """
    itype = _item_type(item_type)
    data = await _read_package(file, itype)
    artifact_name = file.filename if file else None
    tmp, entries, ins = _unpack(data, itype)
    try:
        _secret_stop(tmp / "src", entries)
        return _inspection_out(ins, artifact_name=artifact_name)
    finally:
        # 体检不留任何东西 —— 无论是被明文凭据拦下还是正常答完。
        shutil.rmtree(tmp, ignore_errors=True)


# --- 发布 ------------------------------------------------------------------


def _resolve_category(db: Session, eid: str, category_id: str | None) -> tuple[str, str]:
    """和 skill 上传同一条规则:空则按部门归组,未知/伪分类一律拒。"""
    cid = (category_id or "").strip()
    if not cid:
        cid = group_for_eid(db, eid) or ""
    if not cid:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "请选择一个分组:你的部门没有对应的分组。",
        )
    if cid in PSEUDO_CATEGORY_IDS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"'{cid}' 不是可上传的分组。")
    row = db.get(RpAiCategory, cid)
    if row is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"未知分组 '{cid}'。")
    return cid, row.label


def _ensure_user(db: Session, eid: str) -> None:
    """确保 rp_ai_portal 里有这个人 —— 版本行的 uploader_eid 是指向它的外键。

    **必须按 eid 查,不能用 db.get()。** 这张表的主键是自增的 `id`,`eid` 只是一个带唯一索引
    的普通列,所以 `db.get(RpAiPortal, "EZHGMIO")` 是拿一个 EID 当整数主键去查 —— 永远答
    None,于是每次上传都插一行重复的 eid,被 MySQL 用
    `1062 Duplicate entry 'EZHGMIO' for key 'ix_rp_ai_portal_eid'` 拒掉,上传者看到的是
    400「Database constraint violation」。

    这段代码在生产从未跑到过(推送在它之前就失败了),所以这个洞从上线一直活到现在;同一个
    仓里另外三个 _ensure_user 都是按 eid 查的,只有这一份复制的时候丢了 where。
    """
    if db.scalar(select(RpAiPortal).where(RpAiPortal.eid == eid)) is None:
        db.add(RpAiPortal(eid=eid))
        db.flush()


@router.post("", status_code=status.HTTP_201_CREATED)
async def publish_package(
    request: Request,
    db: Session = Depends(get_db),
    eid: str = Form(""),
    name: str = Form(...),
    description: str | None = Form(None),
    category_id: str | None = Form(None, alias="categoryId"),
    tags: str | None = Form(None),
    launch: str = Form(...),
    runtime: str = Form("oneshot"),
    keep: str | None = Form(None, description="JSON 数组或换行分隔"),
    prerequisites: str | None = Form(None, description="JSON 数组或换行分隔"),
    item_id: str | None = Form(None, alias="itemId"),
    item_type: str = Form("agent", alias="itemType"),
    file: UploadFile | None = File(None),
    x_user_eid: str | None = Header(None),
) -> dict:
    """建项目(或复用已有的)、推一版、写市场条目与版本行。

    **条目直接 `approved`,传完即上市。** 原来是 `pending`,等 admin 批。改掉的理由是那个
    闸门的口径反了、而且它不运转:

    - 反了:登记一个仓库地址(它可以指向任何仓库,内容明天还能改)是即时上市的,而门户唯一
      能把完整文件清单摆在审核人面前的这条路要等审批。门槛加在了信息最充分的那条路上。
    - 不运转:能批的只有 `audit_admin_eid` 一个 EID(默认还是 break-glass 那个账号),而且
      上传后**没有任何通知**。实测生产库:15 条市场条目全是 approved,`pending` 这个状态
      从上线到现在没有作用在任何一行上 —— 因为上传在今天之前从没成功过。第一个真的落进
      pending 的包会直接卡死在那儿。
    - 而它还把人赶向更不安全的那条路:提交完看到"等待审核"、然后什么都不发生,下次就去
      登记指针了。

    换来的口径是"同事放的,自己看",不是"公司审过":每一行都有 creator、每一版都有 GitLab
    提交,出问题能追到人,作者和管理员都能自己下架。`pending` 这个状态仍然存在(管理员在
    审核页拒绝就是它),可见性过滤也照旧,所以下架是即时生效的。

    skill 那条路的状态由它自己的审计闸门决定,不受这里影响。
    """
    # 身份:签名的 JWT 优先(生产),表单字段与请求头只在 auth_enabled=false 时兜底。两者都收,
    # 是因为市场那边的接口用头、skill 上传用表单字段,而这个端点同时被两种调用方式碰到。
    acting = acting_eid(request, eid or x_user_eid)
    if not acting:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "缺少上传者身份")
    itype = _item_type(item_type)
    # 授权在环境检查**之前**。谁能改这个条目是关于这个请求的事实,凭据配没配是关于这次部署
    # 的事实 —— 反过来的话,一个越权请求在部署没配好时会拿到 503「服务不可用」,既误导上传者,
    # 也把一次越权尝试记成了一次故障。
    existing = db.get(RpAiMarketItem, item_id) if item_id else None
    # `creator` 为空时**不放行**。原来是 `existing.creator and ...`,于是同步进来的条目
    # (creator 是 NULL、id 又是可推出来的 `gl-<项目id>`)谁都能推新版本上去,而页面上作者
    # 一个字不变 —— 那是内容替换,不是"编辑一个没人认领的条目"。
    if existing is not None and existing.creator != acting:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "只有原上传者可以上传这个条目的新版本"
            if existing.creator
            else "这个条目没有登记上传者,不能通过上传替换它的内容",
        )
    if existing is not None and existing.item_type != itype:
        # 换类型等于换了它在市场里是什么东西,而卡片、筛选和用法说明都跟着这一格走。静默接受
        # 会让一个 tool 的新版本把它变成 agent,而上传者只是传了一个包。
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"这个条目是 {existing.item_type},不能作为 {itype} 上传新版本。",
        )
    target = glw.target_for(itype)
    if not target.token:
        # 早点说清楚:凭据没配的时候,包收下来也无处存,答 503 比收下再丢掉诚实。
        # 说出**哪一个**变量:现在有两个组两个 token,笼统一句"未配置"会让人去改另一个。
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"上传 {itype} 包所需的 GitLab 凭据({target.env_var})未配置,请联系管理员。",
        )
    try:
        # 签名密钥的存在性在**建项目之前**查。放到 publish_tree 里查的话,一个部署漏挂的
        # 配置错误会先建出一个 GitLab 项目、再把它归档 —— 组里多一个半成品,而原因和这次
        # 上传的内容毫无关系。
        glw.signing_args()
    except glw.GitLabWriteError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, exc.detail) from exc
    launch = launch.strip()
    if not launch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请填写启动命令")
    if runtime not in pkg.RUNTIMES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"runtime 只能是 {pkg.RUNTIMES}")

    data = await _read_package(file, itype)
    artifact_name = file.filename if file else None
    tmp, entries, ins = _unpack(data, itype)
    try:
        _secret_stop(tmp / "src", entries)

        cid, category_label = _resolve_category(db, acting, category_id)
        keep_list = _parse_keep(keep)
        prereq_list = _parse_keep(prerequisites)
        declared = pkg.Declared(
            launch=launch, runtime=runtime, keep=keep_list, prerequisites=prereq_list
        )

        # 这里不跑安全审计:它对包从来不改流向,产出只有一份报告,而包的闸门是人工审核。
        # 详见 inspect_package 的说明。skill 那条路不受影响。

        version = bump_minor(existing.version if existing else None)

        # 自描述文件在 push 之前写进工作目录。
        src = tmp / "src"
        sidecar_dir = src / ins.top_dir if ins.top_dir else src
        (sidecar_dir / pkg.SIDECAR_NAME).write_text(
            pkg.render_sidecar(declared, name=name, version=version), encoding="utf-8"
        )

        if existing is not None and existing.id.startswith("gl-"):
            project = glw.ProjectRef(
                id=int(existing.id[3:]),
                path_with_namespace="",
                web_url=existing.repo_url or "",
                http_url=_http_url_from_web(existing.repo_url or ""),
            )
        else:
            # 建项目也要映射异常。相邻的 `publish_tree` 一直有映射,只有这一步没有 ——
            # 于是 GitLab 的任何 400 都一路冒到 ASGI 层变成裸 500「Unknown Error」,
            # 真正的原因只留在 backend.log 里,上传者看到的那句话没有任何信息量。
            try:
                project = glw.create_project(
                    slug=_slug(name), name=name, description=description or None,
                    target=target,
                )
            except glw.CredentialExpired as exc:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE, exc.detail
                ) from exc
            except glw.GitLabWriteError as exc:
                # 名字/路径被占或不合规矩是**上传者能自己解决**的事(换个名字),所以是 400
                # 而不是 502:502 会让人以为再点一次就好。真正的上游故障(连不上)带
                # `retryable`,那一类才答 503。
                code = (
                    status.HTTP_503_SERVICE_UNAVAILABLE
                    if exc.retryable
                    else status.HTTP_400_BAD_REQUEST
                )
                raise HTTPException(code, exc.detail) from exc

        try:
            sha, tag = glw.publish_tree(
                project, src, version=version,
                message=f"{'upload' if existing is None else 'update'}({cid}): {name} v{version}",
                tmp_root=tmp, target=target,
            )
        except glw.GitLabWriteError as exc:
            if existing is None:
                # 项目建好了但内容没推上去。这个分支**只可能**是本次请求新建的项目(外层
                # `existing is None`),复用已有项目那条路径永远走不到这里。
                #
                # 零提交就删掉,不要归档:归档不释放名字和路径,于是这个包的名字被永久占死,
                # 下一个用同名的人连项目都建不出来,而他看到的报错跟真实原因毫无关系(实测
                # 现场:一个 0 提交的归档项目占着 emc-agent)。删除是延迟删除、会立刻改名把
                # 名字腾出来,而这个仓一个提交都没有,没有东西可失去。
                #
                # 原来这里是"先改名再归档",而那个改名用了 `名字 (failed …)` —— 括号是
                # GitLab 项目名的非法字符,所以它从来没成功过,只留下一行 warning。
                if glw.looks_empty(project.id, target):
                    try:
                        glw.delete_project(project.id, target)
                    except glw.GitLabWriteError:
                        log.warning("推送失败后删除空项目 %s 失败", project.id)
                else:
                    # 收到过提交:内容不一定是本次这一份,不删。
                    try:
                        glw.archive_project(project.id, target)
                    except glw.GitLabWriteError:
                        log.warning("推送失败后归档项目 %s 也失败了", project.id)
            # 502 只留给"上游真的坏了"。凭据失效和推送规则拒收都不是那种情况:内容没问题、
            # 请求没问题,是环境里少了一样东西或多了一条规则,而重试同一份内容一定还是失败 ——
            # 答 502 会让上传者以为再点一次就好。
            if isinstance(exc, (glw.CredentialExpired, glw.PushRuleRejected)):
                code = status.HTTP_503_SERVICE_UNAVAILABLE
            else:
                code = status.HTTP_502_BAD_GATEWAY
            raise HTTPException(code, exc.detail) from exc

        _ensure_user(db, acting)
        item = existing
        if item is None:
            item = RpAiMarketItem(id=project.item_id)
            db.add(item)
            item.creator = acting
            item.source = "upload"
            item.item_type = itype
            # 上传出来的条目**就是**一个 code 服务:它有真实的仓库地址。"zip/tar" 是它怎么
            # 来的,不是它是什么 —— 所以那个轴上是 source,不是 service_type。这也让市场里
            # 每一处 `serviceType === 'code' ? repoUrl : serviceUrl` 照旧成立。
            item.service_type = "code"
            item.repo_url = project.web_url
            # 传完即上市 —— 理由见函数开头。仍然写一次而不是靠模型默认值:这一行是这条路的
            # 策略,读代码的人应该在这里看到它,而不是去翻模型。
            item.status = "approved"
        item.name = name
        if description is not None:
            item.description = description
        item.category_id = cid
        item.category = category_label
        item.tags = _parse_tags(tags)
        item.author = item.author or acting
        item.version = version
        db.flush()

        db.add(RpAiMarketItemVersion(
            item_id=item.id, version=version, commit_sha=sha, tag=tag,
            uploader_eid=acting, artifact_name=artifact_name, artifact_bytes=len(data),
            metadata_json={
                "launch": launch, "runtime": runtime, "keep": keep_list,
                "prerequisites": prereq_list,
                # 门户推出来的安装步骤,和这一版内容绑在一起存 —— 下一版重新推,这一行不动。
                # 提示词读的是这里,不在复制按钮那一刻重新解析一遍包(那要重新下载解压)。
                "install": [
                    {"command": s.command, "note": s.note} for s in ins.install_steps
                ],
            },
            # audit_json 留空:包不再跑安全审计。历史行里那份留痕不动,审核页照旧显示它。
        ))
        db.commit()
        db.refresh(item)

        return {
            "id": item.id,
            "status": item.status,
            "version": version,
            "tag": tag,
            "commitSha": sha,
            "repoUrl": item.repo_url,
            "reviewPath": f"/market/{item.id}/review",
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _http_url_from_web(web_url: str) -> str:
    return f"{web_url.rstrip('/')}.git" if web_url and not web_url.endswith(".git") else web_url


def _slug(name: str) -> str:
    import re

    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return (slug or f"pkg-{uuid.uuid4().hex[:8]}")[:60]


def _parse_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    raw = raw.strip()
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            return [str(t).strip() for t in parsed if str(t).strip()]
        except json.JSONDecodeError:
            pass
    return [t.strip() for t in raw.replace("，", ",").split(",") if t.strip()]


def _parse_keep(raw: str | None) -> list[str]:
    if not raw:
        return []
    raw = raw.strip()
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            return [str(k).strip() for k in parsed if str(k).strip()]
        except json.JSONDecodeError:
            pass
    return [line.strip() for line in raw.splitlines() if line.strip()]

