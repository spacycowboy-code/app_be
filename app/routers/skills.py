"""Skill endpoints: browse, detail, create, update, delete."""
import io
import re
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.favorite import RpAiUserFavorite
from app.models.category import RpAiCategory
from app.models.skill import (
    RpAiInputParam,
    RpAiSkill,
    RpAiSkillCase,
    RpAiSubSkill,
)
from app.models.version import RpAiSkillVersion
from app.models.download import RpAiSkillDownload
from app.repositories import skill_content
from app.schemas.skill import (
    SkillCreate,
    SkillRead,
    SkillSummary,
    SkillUpdate,
)
from app.services.skill_manifest import (
    build_manifest,
    has_name_and_description,
    parse_frontmatter,
    with_frontmatter,
)
from app.services.archives import common_top_dir
from app.services.card_group import groups_for_eids
from app.services.skill_storage import sanitize_segment, skill_storage
from app.services import gitlab_mr
from app.services.audit_engine import AuditResult, audit_skill
from app.services.skill_audit import gate
from app.services.skill_audit import pending as pending_updates
from fastapi.concurrency import run_in_threadpool
from app.services.notify import notify_version_update
from app.services.eids import acting_eid, normalize_eid
from app.services.usage import department_of

router = APIRouter(prefix="/api/skills", tags=["skills"])
settings = get_settings()


def _read_skill_md(skill: RpAiSkill) -> str:
    """按这一行记着的引用读 SKILL.md。

    **读不到就 503,不回落到 `skill.instructions`。** 那一列是同一份正文的第二份副本,
    回落等于把可能过期的内容当成当前版本拿去编辑或审计,而且从响应上看不出来。
    生产 22 个技能的 `storage_path` 与 `repo_commit` 都有值,所以这条回落对现存数据从不生效。
    """
    if not skill.storage_path or not skill.repo_commit:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="这个技能没有记下内容在仓库里的位置或版本,无法读取正文",
        )
    try:
        return skill_content.read_text(
            skill.repo_commit, f"{skill.storage_path}/SKILL.md"
        )
    except skill_content.ContentUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="暂时读不到技能内容,请稍后重试",
        ) from exc


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or uuid.uuid4().hex[:12]


def _get_skill_or_404(db: Session, skill_id: str) -> RpAiSkill:
    skill = db.get(RpAiSkill, skill_id)
    if skill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Skill '{skill_id}' not found")
    return skill


def _apply_children(skill: RpAiSkill, payload) -> None:
    """Replace child collections from a create/update payload when provided."""
    if getattr(payload, "cases", None) is not None:
        skill.cases = [
            RpAiSkillCase(title=c.title, image=c.image) for c in payload.cases
        ]
    if getattr(payload, "sub_skills", None) is not None:
        skill.sub_skills = [
            RpAiSubSkill(
                name=s.name,
                description=s.description,
                badge=s.badge,
                icon_color=s.icon_color,
                icon_bg=s.icon_bg,
            )
            for s in payload.sub_skills
        ]
    if getattr(payload, "input_params", None) is not None:
        skill.input_params = [
            RpAiInputParam(
                name=p.name,
                type=p.type,
                required=p.required,
                description=p.description,
            )
            for p in payload.input_params
        ]


def _content_updated_at(
    skill: RpAiSkill, upload_count: int, latest_upload: datetime | None
) -> datetime | None:
    """When this skill's content last changed, for the Home feed's "Update" rule.

    ``rp_ai_skill_version`` holds one row per upload, which is the only
    authoritative record of a content change — ``updated_at`` on the skill row is
    a bookkeeping column that moves on any write to it.

    * two or more uploads -> the newest one is a real update;
    * exactly one upload -> that upload *is* the creation, so report the creation
      time. Its version row is stamped seconds later than the skill row (separate
      INSERTs, separate ``CURRENT_TIMESTAMP``), and returning that would announce
      every brand-new skill as updated;
    * no version history at all -> the official imports, which never write one.
      Fall back to ``updated_at``: it is trustworthy for them now that the import
      writes a per-folder commit sha rather than the repo-wide HEAD.
    """
    if upload_count >= 2 and latest_upload is not None:
        return latest_upload
    if upload_count == 1:
        return skill.created_at
    return skill.updated_at


@router.get("", response_model=list[SkillSummary])
def list_skills(
    db: Session = Depends(get_db),
    category_id: str | None = Query(None, alias="categoryId"),
    status_filter: str | None = Query(None, alias="status"),
    q: str | None = Query(None, description="search in name/description"),
    trending: bool | None = Query(None),
    x_user_eid: str | None = Header(None),
) -> list[RpAiSkill]:
    stmt = select(RpAiSkill)
    if category_id and category_id != "all":
        stmt = stmt.where(RpAiSkill.category_id == category_id)
    if status_filter:
        stmt = stmt.where(RpAiSkill.status == status_filter)
    if trending is not None:
        stmt = stmt.where(RpAiSkill.is_trending.is_(trending))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            RpAiSkill.name.like(like) | RpAiSkill.description.like(like)
        )
    # Visibility is gated by the lifecycle field only: a skill awaiting review
    # (or rejected by it) is not public. The audit verdict is evidence for why,
    # not the gate. Admin sees everything and an uploader still sees their own.
    # (A danger verdict is never stored at all.)
    requester = normalize_eid(x_user_eid)
    if requester != settings.audit_admin_eid:
        stmt = stmt.where(
            (RpAiSkill.status == "approved")
            | (RpAiSkill.uploader_eid == requester)
        )
    skills = list(db.scalars(stmt).all())
    # Attach the deduplicated like count (favorites are unique per (eid, skill)).
    if skills:
        ids = [s.id for s in skills]
        counts = dict(
            db.execute(
                select(RpAiUserFavorite.skill_id, func.count(RpAiUserFavorite.id))
                .where(RpAiUserFavorite.skill_id.in_(ids))
                .group_by(RpAiUserFavorite.skill_id)
            ).all()
        )
        # Upload history per skill: how many uploads, and when the newest landed.
        # The count is what decides whether the content ever changed — a single
        # upload is the creation itself, however many seconds after the skill row
        # its version row happened to be INSERTed (``upload.py`` flushes the skill
        # first, then commits the version row after the audit record and, for a
        # suspect upload, a GitLab round-trip). Comparing the two timestamps
        # directly would mark every brand-new skill as updated.
        version_stats = {
            sid: (int(n), latest)
            for sid, n, latest in db.execute(
                select(
                    RpAiSkillVersion.skill_id,
                    func.count(RpAiSkillVersion.id),
                    func.max(RpAiSkillVersion.created_at),
                )
                .where(RpAiSkillVersion.skill_id.in_(ids))
                .group_by(RpAiSkillVersion.skill_id)
            ).all()
        }
        for s in skills:
            s.favorite_count = int(counts.get(s.id, 0))
            s.content_updated_at = _content_updated_at(s, *version_stats.get(s.id, (0, None)))
        _attach_group(db, skills)
    return skills


def _attach_group(db: Session, skills: list[RpAiSkill]) -> None:
    """Attach the uploader's org group (地址 · 大部门 · 小部门) onto each skill.

    Keyed on ``uploader_eid`` rather than ``author``: for the official imports
    ``author`` is a label like "Anthropic", which is not a person and has no
    office. Those skills get no group, which is the truthful answer.
    """
    groups = groups_for_eids(db, {s.uploader_eid for s in skills if s.uploader_eid})
    for s in skills:
        s.group = groups.get((s.uploader_eid or "").strip().upper())


@router.get("/{skill_id}", response_model=SkillRead)
def get_skill(skill_id: str, db: Session = Depends(get_db)) -> SkillRead:
    skill = _get_skill_or_404(db, skill_id)
    skill.favorite_count = db.scalar(
        select(func.count())
        .select_from(RpAiUserFavorite)
        .where(RpAiUserFavorite.skill_id == skill_id)
    ) or 0
    upload_count, latest_upload = db.execute(
        select(
            func.count(RpAiSkillVersion.id),
            func.max(RpAiSkillVersion.created_at),
        ).where(RpAiSkillVersion.skill_id == skill_id)
    ).one()
    skill.content_updated_at = _content_updated_at(
        skill, int(upload_count or 0), latest_upload
    )
    _attach_group(db, [skill])
    # 正文按引用从仓库读,不发数据库里那一列 —— 同一份正文两个来源,而响应上看不出用了哪一份。
    #
    # 先把模型构出来再改字段,不给 ORM 对象赋值:`instructions` 是真列,赋值会把 session 弄脏,
    # 有可能被写回数据库。
    #
    # 读不到就 503,和 `/markdown` 一致:这时候页面本来也显示不出正文(它走的就是 `/markdown`),
    # 而把这一格填成空会让复核界面把"读不到"算成"这个技能没写文档"。
    out = SkillRead.model_validate(skill)
    out.instructions = _read_skill_md(skill) if skill.storage_path else None
    return out


@router.get("/{skill_id}/markdown")
def get_skill_markdown(skill_id: str, db: Session = Depends(get_db)) -> dict:
    """Return the skill's full SKILL.md content (the source of truth)."""
    skill = _get_skill_or_404(db, skill_id)
    return {"skillId": skill.id, "content": _read_skill_md(skill)}


def _artifact_member(tree: "dict[str, bytes]") -> "bytes | None":
    """作者上传的那个原始 zip(在 ``artifacts/`` 下),没有就返回 None。

    "哪个文件算作者的原始包"是产品规则,不是存储事实,所以留在路由层:仓储只负责把这一版的文件
    交出来,不判断它们各自是什么角色。
    """
    zips = sorted(k for k in tree if k.startswith("artifacts/") and k.endswith(".zip"))
    return tree[zips[0]] if zips else None


def _common_top_dir(members: list[zipfile.ZipInfo]) -> "str | None":
    """The single top-level directory shared by every archive member, else
    ``None``. Used to strip a redundant wrapper folder (e.g.
    ``tavily-web-search/``) so the download isn't double-nested.

    Delegates to ``services.archives`` so this rule has one implementation: the
    market download endpoint applies the same rule to GitLab's
    ``<project>-<ref>-<sha>`` wrapper, and two copies would drift."""
    return common_top_dir(members)


def _bump_downloads(db: Session, skill: RpAiSkill, eid: str | None = None) -> None:
    """Record a download: log a timestamped event row (with the downloader's
    EID when known, for per-user leaderboards) and increment the cumulative
    counter. Best-effort — a logging failure must never break the download.

    A download is NOT a modification of the skill, so this must not move
    ``updated_at``. That column carries ``onupdate=func.now()`` (TimestampMixin),
    which fires on any UPDATE that does not name it — and the Home page's
    "What's New" feed reports a skill as updated whenever
    ``updated_at > created_at``, so bumping the counter through the ORM made
    every download surface as a bogus "Update" card with an unchanged version.
    Naming ``updated_at`` in the SET clause suppresses ``onupdate`` and carries
    the existing value forward, so only real edits move it.
    """
    downloader = (eid or "").strip() or None
    try:
        next_count = str(int(str(skill.downloads or "0")) + 1)
    except (ValueError, TypeError):
        # Non-numeric counter (e.g. "5.6K"): keep the event row but leave the
        # counter alone rather than guess at its numeric value.
        next_count = None
    try:
        db.add(
            RpAiSkillDownload(
                skill_id=skill.id,
                eid=downloader,
                department=department_of(db, downloader),
            )
        )
        if next_count is not None:
            db.execute(
                update(RpAiSkill)
                .where(RpAiSkill.id == skill.id)
                .values(downloads=next_count, updated_at=RpAiSkill.updated_at)
            )
            # The Core UPDATE bypassed the ORM; drop the now-stale attributes so
            # a later read in this session sees the stored values.
            db.expire(skill, ["downloads", "updated_at"])
        db.commit()
    except Exception:
        db.rollback()


def _content_disposition(filename: str) -> str:
    """Build an attachment Content-Disposition with an ASCII fallback plus an
    RFC 5987 UTF-8 name, so non-ASCII (e.g. Chinese) skill names download with
    the correct filename across browsers."""
    ascii_name = filename.encode("ascii", "ignore").decode().strip() or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


@router.get("/{skill_id}/download")
def download_skill(
    skill_id: str,
    eid: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """Download a skill as a .zip archive named after the skill's real name.

    ``eid`` (optional) records who downloaded, for per-user leaderboards; the
    endpoint stays usable anonymously when it is absent.

    The archive contains the whole skill folder (SKILL.md + any artifacts +
    manifest) under a top-level directory named after the skill. Skills with
    no folder on disk fall back to a zip containing a single SKILL.md built
    from the stored instructions.
    """
    skill = _get_skill_or_404(db, skill_id)
    # Official (anthropic) skills: name the archive after the namespaced id
    # (e.g. "anthropics-xlsx"); user skills after the display name.
    is_official = skill.category_id == "official"
    safe_name = (
        sanitize_segment(skill.id, skill_id)
        if is_official
        else sanitize_segment(skill.name, skill_id)
    )
    if not skill.storage_path or not skill.repo_commit:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "这个技能没有记下内容在仓库里的位置或版本,无法下载",
        )
    try:
        tree = skill_content.read_tree(skill.repo_commit, skill.storage_path)
    except skill_content.ContentUnavailable as exc:
        # 原来这里会用 skill.instructions 合成一个只有 SKILL.md 的 zip。那是同一份正文的第二份
        # 副本,而且下载下来的包看不出它缺了脚本和附件 —— 用户会以为自己拿到了完整的技能。
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "暂时读不到技能内容,请稍后重试"
        ) from exc

    buf = io.BytesIO()
    wrote_any = False
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        seen: set[str] = set()

        def _add(rel: str, data: bytes) -> None:
            nonlocal wrote_any
            rel = rel.strip("/")
            if not rel:
                return
            arc = str(Path(safe_name) / rel)
            if arc in seen:
                return
            seen.add(arc)
            zf.writestr(arc, data)
            wrote_any = True

        # 1) A user's uploaded artifact archive is the source of truth for the
        #    skill's files: it holds the author's original folder (SKILL.md +
        #    scripts + references). Ship its contents, stripping the single
        #    top-level wrapper dir so the skill isn't double-nested under
        #    <name>/<name>/SKILL.md. The loose root SKILL.md is only the
        #    extracted definition; manifest.json / artifacts/ are marketplace-
        #    only and must not leak into the downloaded skill.
        artifact = _artifact_member(tree) if not is_official else None
        if artifact is not None:
            try:
                with zipfile.ZipFile(io.BytesIO(artifact)) as src:
                    members = [m for m in src.infolist() if not m.is_dir()]
                    top = _common_top_dir(members)
                    for m in members:
                        rel = m.filename[len(top) + 1 :] if top else m.filename
                        _add(rel, src.read(m))
            except zipfile.BadZipFile:
                # Corrupt / not-really-a-zip artifact: fall back to loose files.
                pass

        # 2) No usable artifact archive: ship the loose skill files, dropping
        #    the marketplace-only manifest.json and the artifacts/ directory.
        if not wrote_any:
            for rel in sorted(tree):
                parts = Path(rel).parts
                if (parts and parts[0] == "artifacts") or Path(rel).name == "manifest.json":
                    continue
                _add(rel, tree[rel])

        # 3) 仓库里那一版真的什么都没有:报错,不拿数据库里的副本凑一个包出来。
        if not wrote_any:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "该 Skill 没有可下载的内容"
            )

    buf.seek(0)
    _bump_downloads(db, skill, eid)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": _content_disposition(f"{safe_name}.zip")},
    )


@router.post("", response_model=SkillRead, status_code=status.HTTP_201_CREATED)
def create_skill(payload: SkillCreate, db: Session = Depends(get_db)) -> RpAiSkill:
    skill_id = payload.id or _slugify(payload.name)
    if db.get(RpAiSkill, skill_id):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Skill '{skill_id}' already exists"
        )
    data = payload.model_dump(exclude={"id", "cases", "sub_skills", "input_params"})
    skill = RpAiSkill(id=skill_id, **data)
    _apply_children(skill, payload)
    db.add(skill)
    db.commit()
    db.refresh(skill)
    return skill


@router.put("/{skill_id}", response_model=SkillRead)
async def update_skill(
    skill_id: str, payload: SkillUpdate, request: Request, db: Session = Depends(get_db)
) -> RpAiSkill:
    # Who is editing is the signed-in caller, not a field in the body (see acting_eid).
    editor_eid = acting_eid(request, payload.editor_eid)
    skill = _get_skill_or_404(db, skill_id)
    scalar_data = payload.model_dump(
        exclude_unset=True,
        exclude={"cases", "sub_skills", "input_params", "editor_eid", "skill_md"},
    )
    for key, val in scalar_data.items():
        setattr(skill, key, val)
    # author always stores the uploader's EID (identity), never a display name;
    # the human display name is resolved from the user record at read time.
    skill.author = skill.uploader_eid or editor_eid or skill.author
    # When the group (category) is changed, refresh the denormalized label.
    if "category_id" in scalar_data and skill.category_id:
        cat = db.get(RpAiCategory, skill.category_id)
        if cat:
            skill.category = cat.label
    _apply_children(skill, payload)

    # If the raw SKILL.md was edited directly, it is the source of truth:
    # write it verbatim and sync name/description from its frontmatter.
    if payload.skill_md is not None:
        content = payload.skill_md
        if not has_name_and_description(content):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "SKILL.md 必须包含 name: 和 description: 两个字段",
            )
        fm_name, fm_desc = parse_frontmatter(content)
        if fm_name:
            skill.name = fm_name
        if fm_desc is not None:
            skill.description = fm_desc
        # 同上:不再写 instructions。
        md_content = content
    else:
        # 只改了元数据:正文取仓库里现有的那一份,按引用读。
        #
        # 原来这里用 `render_skill_md(skill)`,它读 `skill.instructions`;那一列不再写之后它会
        # 走到"用 name/description 合成一个正文"的回落上 —— 于是这次审计审的是一份合成出来的东西,
        # 而不是仓库里真正存着的内容,审计结论也就不对应任何真实内容。
        md_content = _read_skill_md(skill)
        if not has_name_and_description(md_content):
            # 仓库里那份没有 frontmatter(老内容):补上当前的 name/description 再送审,
            # 与上传路径的处理一致。
            md_content = with_frontmatter(md_content, skill.name, skill.description)

    db.flush()  # make edited fields/children visible for manifest building

    # The same audit as an upload, on the same content, before anything is
    # written. This route used to write straight to the skill repo: the audit was
    # attached to the upload route instead of to the act of writing, so editing a
    # skill was a way to put unaudited content in the repo.
    editor = editor_eid or skill.uploader_eid
    audit: AuditResult = await run_in_threadpool(
        lambda: audit_skill(
            name=skill.name,
            skill_md=md_content,
            uploader_eid=editor,
        )
    )
    decision = gate.decide(audit=audit, skill_id=skill.id,
                           replaces_published=skill.status == "approved")

    if decision.unavailable:
        db.rollback()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "安全审计暂时不可用，本次编辑未保存，请稍后重试。",
        )

    if decision.blocked:
        # Nothing is written and the edit itself is discarded, but the record of
        # the rejection is kept: "why was this refused" has to be answerable.
        #
        # The record is deliberately not linked to the skill. The projection takes
        # the skill's displayed verdict from its latest linked record, so linking a
        # rejected *edit* would mark a live skill as dangerous while what is
        # published is still the older content that passed. A rejected edit says
        # nothing about the published content; the skill name keeps it traceable.
        db.rollback()
        gate.record(db, decision.ref, audit, skill_id=None,
                    skill_name=skill.name, uploader_eid=editor)
        db.commit()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"编辑被安全审计拒绝：{audit.reason or '命中安全红线'}。参考编号 {decision.ref}",
        )

    # 这次改动的内容(改完之后的样子),在技能行被回滚之前抄下来。只有要走人工复核、且这个技能
    # 已经发布过的时候才用得上;抄一份的代价是几个字段的读,所以不分支。
    staged = pending_updates.capture(skill)
    staged_name = skill.name

    # Persist the edit into the git-managed skill repo: refresh the folder
    # files, bump the version, commit, tag and push (same flow as upload).
    owner_eid = skill.uploader_eid or "ericsson"
    slug = sanitize_segment(skill.category_id or "uncategorized", "uncategorized")
    result = skill_storage.store(
        skill_id=skill.id,
        eid=owner_eid,
        name=skill.name,
        category_slug=skill.category_id or "uncategorized",
        manifest=build_manifest(skill, owner_eid, slug),
        instructions=md_content,
        artifact_bytes=None,
        artifact_filename=None,
        current_version=skill.version,
        existing_storage_path=skill.storage_path,
        audit=audit,
        action="edit",
        target_branch=decision.review_branch,
    )

    if decision.stays_published:
        # 要复核、而且这个技能已经在线上:技能行一个字不动 —— 名称、版本、``repo_commit``、状态
        # 全部继续指向已发布的那一版,它照常留在市场里。这次更新整份停在暂存表,等复核 MR 合了
        # 再搬过去(见 skill_audit.pending)。
        #
        # 回滚是为了把改动从技能行上撤下来:改动是先落在 ORM 对象上再送审的(审的就是它渲染出来
        # 的内容),不撤就会随下面的 commit 一起写进去。审计记录还没落库,所以回滚不丢事实。
        db.rollback()
        skill = _get_skill_or_404(db, skill_id)
    else:
        skill.version = result.version
        skill.storage_path = result.storage_path
        skill.repo_commit = result.commit_sha
        # 没有已发布版本可以保护的时候,老规矩:通过即上线,可疑则等复核 MR 合。
        skill.status = decision.status
        skill.audit_state = audit.state
        # 哪条记录描述这一版(见 models/skill.py)。
        skill.audit_ref = decision.ref

        db.add(
            RpAiSkillVersion(
                skill_id=skill.id,
                version=result.version,
                commit_sha=result.commit_sha,
                tag=result.tag,
                uploader_eid=editor_eid or owner_eid,
                notes="Edited via portal",
            )
        )
        # 收藏者收到的"更新到 vX"必须对应一个装得到的版本,所以这条通知只在内容真的发布时发。
        notify_version_update(db, skill, result.version, actor_eid=editor_eid)

    # A review is a person reading a change, so the change has to be somewhere
    # they can read it: same Draft MR as an upload takes.
    mr_url = None
    if decision.needs_review:
        try:
            mr_url = await run_in_threadpool(
                lambda: gitlab_mr.review_suspect(
                    source_branch=decision.review_branch,
                    target_branch=settings.skill_repo_branch,
                    skill_name=staged_name,
                    findings=audit.findings,
                    audit_ref=decision.ref,
                    uploader=gate._uploader_label(db, editor),
                    reason=audit.reason,
                    coverage=audit.coverage,
                    cause=audit.cause,
                )
            )
        except gitlab_mr.ReviewRequestFailed as exc:
            # 复核 MR 开不出来就当这次发布没成功:把刚推的复核分支收回去,让请求失败。
            # 否则内容留在分支上、数据库那一行是 pending,而没有人被通知、也没有东西可复核 ——
            # 上传者看到的"等待复核"是假的。数据库还没提交,所以这里抛出去不会留下半个状态。
            skill_content.delete_branch(decision.review_branch)
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "内容需要人工复核,但复核单没能创建,本次未保存,请稍后重试。",
            ) from exc

    if decision.stays_published:
        pending_updates.stage(
            db, skill_id=skill.id, staged=staged, audit_ref=decision.ref,
            review_branch=decision.review_branch, mr_url=mr_url,
            version=result.version, storage_path=result.storage_path,
            commit_sha=result.commit_sha, tag=result.tag,
            artifact_name=result.artifact_name, editor_eid=editor_eid or owner_eid,
        )
    elif skill.pending_update is not None:
        # 这次改动直接发布了,它就是线上那一版;之前那份待复核的更新已经不是"线上之外压着的下一
        # 版"了,留着会在复核合并时把作者后来改的东西盖回去。
        pending_updates.drop(db, skill.pending_update, why="被一次直接发布的编辑取代")

    gate.record(db, decision.ref, audit, skill_id=skill.id,
                skill_name=staged_name, uploader_eid=editor, mr_url=mr_url,
                replaces_published=decision.replaces_published)

    db.commit()
    db.refresh(skill)

    # 写成功即内容在 GitLab 上:没有本地提交,也就没有"已保存但没同步"这种状态。写不成会抛,
    # 由 main.py 的处理器答成 409/503,数据库那一行还没写(顺序是先仓库后数据库)。
    # `synced` 字段保留(前端在读),但它现在恒为真;原来那条 sync_warning 分支不可达。
    skill.synced = result.pushed
    return skill


@router.delete("/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_skill(
    skill_id: str,
    request: Request,
    db: Session = Depends(get_db),
    x_user_eid: str | None = Header(None),
) -> None:
    """下架一个技能:把内容从仓库移掉,数据库那一行置成 archived。

    **为什么不删那一行。** 它带着仓库里算不出来的门户事实:下载量、收藏、评价、审计记录。删掉它等于
    为了跟仓库保持一致而销毁门户自己的记录。公开列表按 ``status == "approved"`` 过滤,所以置成
    ``archived`` 之后它自然从市场消失,前端不用改。

    **为什么要动仓库。** 只删数据库那一行的话,manifest 还在仓库里;收敛器补齐 manifest 那一步一上线,
    这个技能会被重新扫回来。内容的权威在仓库,所以"删除"必须先在仓库里发生。

    鉴权照 market 那边的规矩:身份取已验证的调用者(不是调用方能改的请求头),只有上传者本人和管理员
    可以下架。原来这个接口**没有任何鉴权**,任何人可以删任何技能。
    """
    skill = _get_skill_or_404(db, skill_id)
    requester = acting_eid(request, x_user_eid)
    is_admin = requester == settings.audit_admin_eid
    if not is_admin and skill.uploader_eid and requester != skill.uploader_eid:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "只有上传者本人或管理员可以下架这个技能"
        )

    if skill.storage_path and skill.repo_commit:
        try:
            skill_content.publish(
                prefix=skill.storage_path,
                files={},
                expect=skill.repo_commit,
                branch=settings.skill_repo_branch,
                message=f"remove: {skill.name} (下架, 操作者 {requester})",
            )
        except skill_content.NotFound:
            pass  # 仓库里已经没有了,这次下架只需要改状态
    skill.status = "archived"
    db.commit()
