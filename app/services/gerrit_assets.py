"""把 Gerrit 上 ``radio/ai-assets`` 的资产同步进门户。

那个仓是 ``BNEW RTE RP SWD``(北京)和 ``BNEW RTE RA RD5``(另一个组织)共用的资产库,有自己的
规范:每个资产是一个子目录,目录里有一份 ``manifest.yaml``,schema 由仓根的
``manifest.yaml.template`` 定义。门户是它的**只读下游** —— 不写回,不改上游。

三个目录,两张表::

    skill/<name>/   -> rp_ai_skill          (SKILL.md 是正文)
    agent/<name>/   -> rp_ai_market_item    item_type=agent
    tool/<name>/    -> rp_ai_market_item    item_type=tool

``knowledge/`` 与 ``rule/`` 不同步(前者只有一个子目录,后者是空的)。

几个当时定下来、值得写在这里的取舍:

**幂等键是目录名,不是 manifest 里的 id。** 那个 schema 根本没有 id 字段,只有 ``name``,而
实测 19 个资产的 ``name`` 与目录名逐字相同。落到门户主键是 ``gerrit-<kind>-<目录名>``,与
GitLab 同步用 ``gl-<项目id>`` 是同一手法:把外部标识编进主键,一次同步认得出上一次的行。
代价是**上游改目录名等于换资产**,门户会认成"旧的没了、新的来了"。

**上游目录消失时不删门户里的行。** 一次网络抖动、一次权限变化、一次 ``manifest.yaml``
写坏,都会让某个资产在这次扫描里"不见了"。删掉是不可逆的(下载与收藏都是 ``CASCADE``),
而留着一条过期的卡片是可逆的。所以这里只新增和更新,返回体里报告"这次没见到的"让人去看。

**变更判据是 git 的对象 sha,不是版本号。** 19 个资产里 7 个的 ``manifest.yaml`` 根本没有
``version``,拿版本号判断"内容变了没有"对它们永远是"没变"。

**两个 sha 各管一件事,不能混。** ``repo_commit`` 是**提交** sha —— 读文件时路径按它解析;
``content_sha`` 是这个资产**目录自己**的 tree sha —— 只用来判断"这个资产变了没有"。第一版
把目录的 tree sha 写进了 ``repo_commit``,结果读正文时 Gitiles 直接 404:一个 tree sha 不是
路径能解析的东西。反过来两处都用提交 sha 也不行 —— 上游任何一次提交都会让 19 个资产全部
"变更",把每张卡片的时间戳一起刷新。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import yaml
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.category import RpAiCategory
from app.models.market import RpAiMarketItem
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal
from app.repositories import gerrit_content
from app.services.asset_content import GERRIT_PREFIX
from app.services.asset_owner import OwnerResolver, ResolvedOwner
from app.services.dept_groups import resolve_group

log = logging.getLogger(__name__)

#: 目录 -> 落到哪。``skill`` 进技能表,另两个进市场表并带上各自的 item_type。
PLAN: tuple[tuple[str, str], ...] = (
    ("skill", "skill"),
    ("agent", "agent"),
    ("tool", "tool"),
)

#: 同步来的市场条目的来源标记。与 ``manual`` / ``upload`` / ``gitlab-*`` 并列,取一个专门的值
#: 是为了这批行以后能被找出来、批量改、或者整批撤掉。
MARKET_SOURCE = "gerrit-ai-assets"

#: 每个资产目录里那份说明自己是什么的文件。
MANIFEST = "manifest.yaml"

#: 技能正文。没有它的目录不是技能。
SKILL_MD = "SKILL.md"

#: 目录里除资产之外还有这些说明文件,不是资产。
NON_ASSET_NAMES = frozenset({"INDEX.md", "README.md", "RULES.md"})


@dataclass
class AssetOutcome:
    kind: str
    directory: str
    item_id: str
    action: str            # created | updated | unchanged | skipped
    reason: str = ""
    creator: str = ""
    author: str = ""
    category_id: str | None = None
    owner_how: str = ""
    owner_fell_back: bool = False


@dataclass
class SyncReport:
    ref: str = ""
    outcomes: list[AssetOutcome] = field(default_factory=list)
    #: 门户里有、这次扫描没见到的 —— 故意不删,列出来让人判断。
    missing: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for o in self.outcomes:
            counts[o.action] = counts.get(o.action, 0) + 1
        return {
            "ref": self.ref,
            "counts": counts,
            "fellBackOwners": [
                {"asset": f"{o.kind}/{o.directory}", "creator": o.creator, "why": o.owner_how}
                for o in self.outcomes
                if o.owner_fell_back
            ],
            "notSeenThisRun": self.missing,
            "assets": [
                {
                    "kind": o.kind,
                    "directory": o.directory,
                    "id": o.item_id,
                    "action": o.action,
                    "reason": o.reason,
                    "creator": o.creator,
                    "author": o.author,
                    "categoryId": o.category_id,
                    "ownerResolvedBy": o.owner_how,
                }
                for o in self.outcomes
            ],
        }


def item_id_for(kind: str, directory: str) -> str:
    return f"gerrit-{kind}-{directory}"


def _manifest_of(ref: str, path: str) -> dict | None:
    """读并解析一个资产的 manifest。解析不了就返回 None,由调用方跳过。

    ``yaml.safe_load`` 而不是 ``load``:这是别人维护的仓库里的文件,构造任意 Python 对象的
    能力不该交给它。
    """
    try:
        raw = gerrit_content.read_text(ref, f"{path}/{MANIFEST}")
    except gerrit_content.NotFound:
        return None
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        log.warning("%s/%s 解析失败: %s", path, MANIFEST, exc)
        return None
    return data if isinstance(data, dict) else None


def _text(value: object) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _capability_text(manifest: dict) -> str | None:
    """把 ``when_to_use`` 与 ``capabilities`` 合成一段能力说明。

    门户的 ``capability_full`` 是详情页那段长描述。manifest 把这件事拆成了两个字段,而
    ``description`` 上游自己限定了 120 字 —— 只拿 description 会丢掉这个资产最有用的部分。
    """
    parts: list[str] = []
    when = _text(manifest.get("when_to_use"))
    if when:
        parts.append(f"适用场景:{when}")
    caps = manifest.get("capabilities")
    if isinstance(caps, list):
        lines = [f"- {_text(c)}" for c in caps if _text(c)]
        if lines:
            parts.append("能力:\n" + "\n".join(lines))
    return "\n\n".join(parts) or None


def _tags_for(kind: str, manifest: dict) -> list[str]:
    """标签只放能从 manifest 确认的事实。

    ``type``(``prompt`` / ``script`` / ``cli-tool`` / ``vscode-extension`` / ``web-tool``)
    是上游自己写的分类,比我们猜的任何标签都可靠;没有这个字段的资产就只带 kind。
    """
    tags = [kind]
    t = _text(manifest.get("type"))
    if t:
        tags.append(t)
    return tags


def _ensure_user(db: Session, owner: ResolvedOwner) -> None:
    """让 creator 在 ``rp_ai_portal`` 里有一行,否则报表认不出这个人。

    ``origin='import'`` 而不是 ``login``:这些人没有登录过门户,是同步替他们登记的作品。那一列
    正是为了让"曾登录过的人数"不把他们算进去 —— 见 ``models/user.py``。

    必须按 ``eid`` 查而不是 ``db.get``:这张表的主键是自增的 ``id``,``eid`` 只是个唯一索引列。
    """
    if not owner.eid:
        return
    row = db.query(RpAiPortal).filter(RpAiPortal.eid == owner.eid).one_or_none()
    if row is None:
        db.add(
            RpAiPortal(
                eid=owner.eid,
                display_name=owner.display_name[:128] or None,
                department=(owner.department or "")[:64] or None,
                origin="import",
            )
        )
        db.flush()
        return
    # 已有行只补空缺,不覆盖:那可能是一条真实登录写下的、比目录快照更新的记录。
    if not row.department and owner.department:
        row.department = owner.department[:64]
    if not row.display_name and owner.display_name:
        row.display_name = owner.display_name[:128]


def _category_of(db: Session, owner: ResolvedOwner) -> tuple[str | None, str | None]:
    """``(category_id, label)``,由 owner 的真实部门决定,查不到就留空。

    留空是刻意的:``dept_groups`` 的表只覆盖 ``BNEW RTE RP``,而这个仓一半的作者在
    ``BNEW RTE RA``。把他们塞进某个 RP 的分组是在编造归属,而空归属的后果只是这张卡片
    不出现在部门筛选里 —— 可见、可解释、可以以后改。
    """
    slug = resolve_group(owner.department)
    if not slug:
        return None, None
    row = db.get(RpAiCategory, slug)
    if row is None:
        # 表是代码、分类是数据,两者会漂。给出一个市场没有对应行的 slug 会让每一页都筛成空。
        log.warning("部门 %r 映射到不存在的分类 %r", owner.department, slug)
        return None, None
    return slug, row.label


def _sync_skill(
    db: Session, ref: str, directory: str, manifest: dict, owner: ResolvedOwner, tree_sha: str
) -> AssetOutcome:
    path = f"skill/{directory}"
    skill_id = item_id_for("skill", directory)
    existing = db.get(RpAiSkill, skill_id)
    is_new = existing is None
    skill = existing or RpAiSkill(id=skill_id)

    if not is_new and skill.content_sha == tree_sha:
        return AssetOutcome(
            "skill", directory, skill_id, "unchanged", "内容 sha 未变",
            owner.eid, owner.display_name, skill.category_id, owner.how, owner.fell_back,
        )

    if is_new:
        db.add(skill)
        # 运行期字段只在建行时初始化;之后是门户自己的数据,同步不该碰。
        skill.rating = 0.0
        skill.downloads = "0"
        skill.review_count = 0
        skill.is_trending = False
        skill.is_free = True

    cat_id, cat_label = _category_of(db, owner)
    skill.name = _text(manifest.get("name")) or directory
    skill.description = _text(manifest.get("description"))
    skill.capability_full = _capability_text(manifest)
    skill.category_id = cat_id
    skill.category = cat_label
    skill.tags = _tags_for("skill", manifest)
    skill.version = _text(manifest.get("version"))
    skill.author = owner.display_name
    skill.uploader_eid = owner.eid
    skill.status = "approved"
    # 正文按引用读,不落库:`storage_path` 带 `gerrit:` 前缀,`asset_content` 据此分派到
    # Gerrit 而不是门户自己的技能仓。
    skill.storage_path = f"{GERRIT_PREFIX}{path}"
    # 提交 sha 给读路径用,目录 sha 给"变了没有"用。见模块开头。
    skill.repo_commit = ref
    skill.content_sha = tree_sha
    return AssetOutcome(
        "skill", directory, skill_id, "created" if is_new else "updated", "",
        owner.eid, owner.display_name, cat_id, owner.how, owner.fell_back,
    )


def _sync_market(
    db: Session, ref: str, kind: str, directory: str, manifest: dict,
    owner: ResolvedOwner, tree_sha: str,
) -> AssetOutcome:
    path = f"{kind}/{directory}"
    item_id = item_id_for(kind, directory)
    existing = db.get(RpAiMarketItem, item_id)
    is_new = existing is None
    item = existing or RpAiMarketItem(id=item_id)

    if not is_new and item.content_sha == tree_sha:
        return AssetOutcome(
            kind, directory, item_id, "unchanged", "内容 sha 未变",
            owner.eid, owner.display_name, item.category_id, owner.how, owner.fell_back,
        )

    if is_new:
        db.add(item)
        item.is_trending = False

    cat_id, cat_label = _category_of(db, owner)
    item.name = _text(manifest.get("name")) or directory
    item.description = _text(manifest.get("description"))
    item.item_type = kind
    # 一律 code:实测这 11 个 agent/tool 没有一个是"已经在跑、给个地址就能用"的服务 ——
    # 连带 `web_service/` 的 jesd-agent 也是要自己起的源码。填 cloud 会让"打开"按钮
    # 指向一个不存在的服务。
    item.service_type = "code"
    item.repo_url = gerrit_content.web_url(get_settings().gerrit_assets_branch, path)
    item.service_url = None
    item.embeddable = False
    item.source = MARKET_SOURCE
    item.creator = owner.eid
    item.author = owner.display_name
    item.version = _text(manifest.get("version"))
    item.tags = _tags_for(kind, manifest)
    item.category_id = cat_id
    item.category = cat_label
    item.status = "approved"
    item.verified = True
    item.content_sha = tree_sha
    return AssetOutcome(
        kind, directory, item_id, "created" if is_new else "updated", "",
        owner.eid, owner.display_name, cat_id, owner.how, owner.fell_back,
    )


def sync_ai_assets(db: Session, *, ref: str | None = None) -> dict:
    """扫一遍三个目录并 upsert。写库,但只写门户自己的表。"""
    settings = get_settings()
    head = ref or gerrit_content.head_ref(settings.gerrit_assets_branch)
    report = SyncReport(ref=head)
    resolver = OwnerResolver()
    seen_ids: set[str] = set()

    try:
        for directory_name, kind in PLAN:
            for entry in gerrit_content.list_dir(head, directory_name):
                name = str(entry["name"])
                if entry["type"] != "tree" or name in NON_ASSET_NAMES:
                    continue
                path = f"{directory_name}/{name}"
                manifest = _manifest_of(head, path)
                if manifest is None:
                    report.outcomes.append(
                        AssetOutcome(kind, name, item_id_for(kind, name), "skipped",
                                     f"没有可解析的 {MANIFEST}")
                    )
                    continue
                # 目录自己的 tree sha:目录内任何文件变了它就变,manifest 没变也算变。
                tree_sha = str(entry["id"])
                owner = resolver.resolve(_text(manifest.get("owner")))
                _ensure_user(db, owner)
                if kind == "skill":
                    outcome = _sync_skill(db, head, name, manifest, owner, tree_sha)
                else:
                    outcome = _sync_market(db, head, kind, name, manifest, owner, tree_sha)
                report.outcomes.append(outcome)
                seen_ids.add(outcome.item_id)

        # 上游少了什么就报告什么,但不动库。删一条卡片会连带删掉别人的下载与收藏
        # (三处 ondelete=CASCADE),而那是这次同步唯一不可回溯的动作。
        known_skills = {
            s.id for s in db.query(RpAiSkill).filter(
                RpAiSkill.storage_path.like(f"{GERRIT_PREFIX}%")
            )
        }
        known_items = {
            i.id for i in db.query(RpAiMarketItem).filter(
                RpAiMarketItem.source == MARKET_SOURCE
            )
        }
        report.missing = sorted((known_skills | known_items) - seen_ids)
        db.commit()
    finally:
        resolver.close()
    return report.summary()
