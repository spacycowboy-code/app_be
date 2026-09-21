"""Skill storage service.

Stores each uploaded skill as a folder inside the git-managed skill repo:

    <repo>/<categorySlug>/<id>_<eid>_<name>/
        manifest.json     # structured metadata
        SKILL.md          # skill definition / instructions
        artifacts/        # uploaded raw file(s)

每次上传把版本号推进一格、声明这个目录应有的全部内容、原子写进技能仓、并给这一版起个名字
(``<id>@<version>``)。没有本地工作副本:写不成会抛,不存在"本地提交成功而推送失败"这种状态。
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.repositories import skill_content
from app.services.audit_engine import AuditResult
from app.services.skill_manifest import frontmatter_version

settings = get_settings()

# Characters unsafe for a path segment; keep unicode (Chinese) letters.
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WS = re.compile(r"\s+")


@dataclass
class StoreResult:
    storage_path: str          # relative path within the repo
    version: str
    commit_sha: str | None
    tag: str | None
    artifact_name: str | None
    pushed: bool
    # Where ``version`` came from: "declared" (SKILL.md), "auto" (platform
    # numbering), or "unchanged" (content identical, no new version cut).
    version_source: str = "auto"
    # One sentence for the author when the declared version was passed over.
    # Empty when there is nothing to explain — see ``VersionPlan``.
    version_note: str = ""


def sanitize_segment(text: str, fallback: str = "skill") -> str:
    """Make a string safe to use as a single path segment (keeps unicode)."""
    text = _UNSAFE.sub("", text or "")
    text = text.replace("..", "").strip().strip(".")
    text = _WS.sub("_", text)
    return text[:80] or fallback


def bump_minor(version: str | None) -> str:
    """Bump the minor component: 1.2.0 -> 1.3.0. Default first version 1.0.0."""
    if not version:
        return "1.0.0"
    parts = re.findall(r"\d+", version)
    if not parts:
        return "1.0.0"
    major = int(parts[0])
    minor = int(parts[1]) if len(parts) > 1 else 0
    return f"{major}.{minor + 1}.0"


def version_key(version: str | None) -> tuple[int, int, int]:
    """Comparable key for two version strings. Missing components read as 0.

    Only used to answer "is the declared version newer than what is already
    published". It is intentionally forgiving because ``version`` is free text:
    one production skill versions itself ``2026.09.09``. Digits are read in
    order, so that scheme compares sensibly against itself; comparing it against
    a semver scheme is meaningless, but that only happens when an author changes
    scheme mid-stream, and the answer then ("not newer, keep auto-numbering") is
    the safe one either way.
    """
    parts = re.findall(r"\d+", version or "")[:3]
    nums = [int(p) for p in parts] + [0] * (3 - len(parts))
    return (nums[0], nums[1], nums[2])


@dataclass(frozen=True)
class VersionPlan:
    """Which version this upload will publish as, and why.

    ``note`` is written for the author and shown in the upload wizard **before
    publishing**. It is empty when there is nothing to explain: an author who
    declares no version, or whose declared version is simply accepted, should
    not be handed a sentence about a mechanism they never touched.
    """

    version: str
    # "declared" = taken from SKILL.md; "auto" = platform's own minor bump.
    source: str
    note: str = ""


def plan_version(
    *, declared: str | None, current: str | None, taken: Iterable[str] = ()
) -> VersionPlan:
    """Decide the version to publish from the author's declared one.

    **Why the author's number wins when it can.** The platform used to number
    every upload itself with ``bump_minor``, which made the badge on the detail
    page disagree with the ``version`` in the author's own SKILL.md — measured on
    production, 12 of the 12 skills that declare a version disagreed, and none
    agreed. Worse, ``bump_minor`` cannot express what only the author knows: it
    answers 1.1.0 where the author means 2.0.0 because this release breaks the
    previous one.

    **Why it does not always win.** Two shapes must not be published verbatim,
    and both exist in production today:

    * a version that was already published — the tag is ``<id>@<version>``, so a
      duplicate is refused by GitLab at the last step, *after* the content is
      already on the branch: a half-published state only visible by looking at
      GitLab.
    * a version older than what is already out — 6 production skills declare one
      (``plda-test-report-bundle`` says 0.5.0 while 1.7.0 is published). Taking
      it would walk the badge and the tag sequence backwards.

    In both cases the platform keeps numbering and ``note`` says which value in
    the file was passed over and why, so the author can fix the file rather than
    discover the drift on the detail page afterwards.

    ``current`` is the basis for auto-numbering and must come from the manifest
    in the content repo, not from the database — see the comment at its call
    site in ``store``. ``taken`` is only a guard, so reading it from the database
    is safe: a stale read at worst misses a duplicate that the tag write then
    catches.
    """
    published = {(v or "").strip() for v in taken if (v or "").strip()}
    auto = bump_minor(current)
    value = (declared or "").strip()

    if not value:
        return VersionPlan(version=auto, source="auto")

    if value in published:
        return VersionPlan(
            version=auto,
            source="auto",
            note=f"SKILL.md 里写的 {value} 已经发布过了，这一版发 {auto}。",
        )

    if current and version_key(value) <= version_key(current):
        return VersionPlan(
            version=auto,
            source="auto",
            note=(
                f"SKILL.md 里写的 {value} 不比已发布的 {current} 新，"
                f"这一版发 {auto}。"
            ),
        )

    return VersionPlan(version=value, source="declared")


class SkillStorage:
    def __init__(self) -> None:
        # 没有本地状态了:内容走仓储层的接口读写。原来这里持有工作副本路径与 git 身份配置。
        pass

    def folder_name(self, skill_id: str, eid: str, name: str) -> str:
        return f"{sanitize_segment(skill_id, 'id')}_{sanitize_segment(eid, 'anon')}_{sanitize_segment(name)}"

    def store(
        self,
        *,
        skill_id: str,
        eid: str,
        name: str,
        category_slug: str,
        manifest: dict,
        instructions: str | None,
        artifact_bytes: bytes | None,
        artifact_filename: str | None,
        current_version: str | None,
        existing_storage_path: str | None,
        audit: AuditResult,
        action: str = "upload",
        target_branch: str | None = None,
        # 这个技能已经发布过的版本号,只用于拦住撞号的声明值(见 `plan_version`)。
        # 默认空集合让老的调用方行为不变:没有已发集合时,声明值只受"要比当前新"这一条约束。
        taken: Iterable[str] = (),
    ) -> StoreResult:
        """Write/refresh a skill folder, bump version, commit, tag and push.

        ``audit`` is required, and required by design: this is the only way
        content reaches the skill repo, so asking for the audit outcome here
        means a caller cannot write unaudited content — including a route added
        later, which is how the edit path came to bypass the check in the first
        place. The two rules the outcome implies are enforced rather than trusted
        to every call site.

        When ``target_branch`` is set (and differs from the live branch), the
        commit is placed on that branch and pushed there — used for 可疑(suspect)
        skills that must not land on the live branch until a review MR is merged.
        """
        # Suspect content goes on its own branch off live, never on live.
        on_branch = bool(target_branch and target_branch != settings.skill_repo_branch)
        if audit.state == "danger":
            raise ValueError(
                "refusing to write content the audit rejected "
                f"(skill_id={skill_id!r})"
            )
        if audit.state == "suspect" and not on_branch:
            raise ValueError(
                "content awaiting review must be written to a review branch, "
                f"not to {settings.skill_repo_branch!r} (skill_id={skill_id!r})"
            )

        category_slug = sanitize_segment(category_slug, "uncategorized")

        # Stable folder: reuse the existing path if the skill already exists.
        if existing_storage_path:
            rel = existing_storage_path
        else:
            rel = f"{category_slug}/{self.folder_name(skill_id, eid, name)}"

        live = settings.skill_repo_branch
        # 基点永远是**远端** live 分支现在这一版。复核分支由 publish 的 start_branch 从它切出来:
        # 原来是本地 fetch + checkout -B,而本地 ref 会漂,从漂了的 ref 切出来的分支 diff 里带着
        # "把这期间合入的东西全 revert 掉" —— GitLab 要么报冲突(运气好),要么干净地合并并悄悄
        # 撤掉已经复核通过的工作。
        base = skill_content.head_ref(live)
        # 这是新建还是更新,**看仓库里有没有,不看数据库有没有那一行**。内容的权威在仓库:
        # 数据库说没有而仓库里有(例如今天之前被删过的技能 —— 那时只删行、内容留着),按"新建"
        # 去写会撞上 400 "A file with this name already exists",而调用方看到的是"暂时无法读写
        # 技能仓",找不到真实原因。
        try:
            before = skill_content.read_tree(base, rel)
        except skill_content.NotFound:
            before = {}

        # 版本号从**仓库里那份 manifest** 推,不从数据库加一:两个并发发布从数据库加一会算出同一个
        # 版本号,而 tag 冲突时代码是默默跳过 —— 两份不同内容共用一个版本号,没人知道。
        # 仓库里没有 manifest(还没迁移过的老技能)才退到调用方给的当前版本。
        version_in_repo: str | None = None
        raw_manifest = before.get("manifest.json")
        if raw_manifest:
            try:
                version_in_repo = json.loads(raw_manifest.decode("utf-8")).get("version")
            except (json.JSONDecodeError, UnicodeDecodeError):
                version_in_repo = None

        # 声明这个目录**应有的全部内容**:先带上原有的文件,再覆盖这次要改的。不这么做的话,
        # publish 会把没出现的文件(作者的脚本、参考资料)当成"要删掉"。
        files: dict[str, bytes] = dict(before)

        files["SKILL.md"] = (instructions or f"# {name}\n").encode("utf-8")

        artifact_name = None
        if artifact_bytes is not None and artifact_filename:
            # Replace any previously stored artifacts so stale files from an
            # earlier version don't leak into downloads.
            for old in [k for k in files if k.startswith("artifacts/")]:
                del files[old]
            artifact_name = sanitize_segment(Path(artifact_filename).name, "artifact")
            files[f"artifacts/{artifact_name}"] = artifact_bytes
        else:
            kept = sorted(k for k in files if k.startswith("artifacts/"))
            artifact_name = Path(kept[0]).name if kept else None

        # 内容没变就不发新版本。
        #
        # 判断只看**作者可见的内容**(SKILL.md 与附件),不看 manifest:manifest 里带着这次要写的版本号,
        # 拿它一起比永远不相等,于是重传一次同样的内容也会涨一格版本、多一个提交。
        # `publish` 里那道"内容一样就不提交"的闸因此永远够不到 —— 闸要放在算版本号之前。
        def _authored(d: dict[str, bytes]) -> dict[str, bytes]:
            return {k: v for k, v in d.items() if k != "manifest.json"}

        if version_in_repo and _authored(before) == _authored(files):
            return StoreResult(
                storage_path=rel,
                version=version_in_repo,
                commit_sha=base,
                tag=None,
                artifact_name=artifact_name,
                pushed=True,
                version_source="unchanged",
            )

        # 版本号优先用作者在 SKILL.md frontmatter 里声明的那个,平台自增只是兜底 ——
        # 判断规则与「为什么」见 `plan_version`。自增基准仍是**仓库里那份 manifest**
        # (上面那段注释),`taken` 只做撞号守卫、可以来自数据库。
        plan = plan_version(
            declared=frontmatter_version(instructions or ""),
            current=version_in_repo or current_version,
            taken=taken,
        )
        version = plan.version

        full_manifest = {
            **manifest,
            "id": skill_id,
            "version": version,
            "uploaderEid": eid,
            "categorySlug": category_slug,
            "artifactName": artifact_name,
        }
        files["manifest.json"] = json.dumps(
            full_manifest, ensure_ascii=False, indent=2
        ).encode("utf-8")

        commit_msg = f"{action}({category_slug}): {name} v{version}"
        sha = skill_content.publish(
            prefix=rel,
            files=files,
            expect=base if before else None,
            branch=target_branch if on_branch else live,
            message=commit_msg,
            start_branch=live if on_branch else None,
        )

        tag_name = f"{sanitize_segment(skill_id, 'id')}@{version}"
        if not skill_content.tag(name=tag_name, ref=sha, message=commit_msg):
            tag_name = None  # 已存在或失败:不让它挡住上传

        return StoreResult(
            storage_path=rel,
            version=version,
            commit_sha=sha,
            tag=tag_name,
            artifact_name=artifact_name,
            # 没有本地提交了:publish 返回即内容在远端。写不成会抛,由调用方决定怎么失败。
            pushed=True,
            version_source=plan.source,
            version_note=plan.note,
        )


skill_storage = SkillStorage()
