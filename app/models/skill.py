"""Skill model: rp_ai_skill, plus normalized child tables.

Mirrors the frontend SkillDetail interface. List-valued display fields
(tags, ratingDist) are stored as JSON; structured collections
(cases, subSkills, inputParams) are normalized into child tables.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.favorite import RpAiUserFavorite
    from app.models.pending_update import RpAiSkillPendingUpdate
    from app.models.review import RpAiSkillReview
    from app.models.user import RpAiPortal
    from app.models.version import RpAiSkillVersion

# Skill review workflow states.
SKILL_STATUSES = ("pending", "approved", "rejected")


class RpAiSkill(TimestampMixin, Base):
    __tablename__ = "rp_ai_skill"

    # Frontend uses string ids (e.g. "tavily-web-search").
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    category: Mapped[str | None] = mapped_column(String(128))
    category_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("rp_ai_category.id", ondelete="SET NULL"), index=True
    )

    image: Mapped[str | None] = mapped_column(String(512))
    # --- 作者自己上传的封面图 -------------------------------------------------
    # 上面那个 ``image`` 是**前端静态资源路径**(skill-images/<部门>.jpg),按部门自动派,作者
    # 对它没有选择权。下面三列是作者自己上传的那一张,存字节。两者并存而不是替换:自定义图被删
    # 掉之后自然回落到部门默认图,老数据也不用迁。
    #
    # **为什么存进库,而不是宿主机上一个目录。** platform-ci 的 backup.sh 只做 MySQL(mysqldump
    # 之后起一个临时实例导进去、逐表比行数验证还原)。新开一个宿主机目录等于凭空造出一份没人备份
    # 的状态,而这些字节是用户提供的、不可重建 —— 丢了就是丢了,而且没人会发现它没被备份,直到那
    # 一天。存进库就自动落在那条已经在跑、且已验证能还原的路径上;顺带不给后端增加新的机器绑定,
    # 多副本也不需要共享盘。代价是库变大,按 512 KB 上限算完全可以忽略。
    #
    # ``deferred=True`` 不是优化,是必需的:列表接口一次返回几十个技能,不 defer 的话每次都会把
    # 每一行的图片字节都拖过来 —— 而那个接口连图都不显示。只有 GET /image 会真的碰它。
    image_blob: Mapped[bytes | None] = mapped_column(
        LargeBinary(length=(1 << 24) - 1), deferred=True
    )
    # 回吐时用的 MIME。它同时充当"有没有自定义图"的判据(见 has_custom_image):判据不用
    # image_blob 是因为读那一列会触发 deferred 加载,把刚才省下的字节又拖回来。
    image_mime: Mapped[str | None] = mapped_column(String(64))
    # 图换过的时间,只用来算 ETag / 缓存校验。
    image_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    rating: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # Frontend displays a formatted string like "5.6K".
    downloads: Mapped[str] = mapped_column(String(32), default="0", nullable=False)
    is_trending: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_free: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    tags: Mapped[list | None] = mapped_column(JSON)  # list[str]
    version: Mapped[str | None] = mapped_column(String(32))
    author: Mapped[str | None] = mapped_column(String(128))

    # Uploader EID -> rp_ai_portal.eid.
    uploader_eid: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("rp_ai_portal.eid", ondelete="SET NULL"), index=True
    )

    capability_full: Mapped[str | None] = mapped_column(Text)
    rating_dist: Mapped[list | None] = mapped_column(JSON)  # list[int], 5 buckets
    review_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    example_prompt: Mapped[str | None] = mapped_column(Text)
    example_output: Mapped[str | None] = mapped_column(Text)
    # Raw skill definition / instructions (markdown).
    instructions: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)

    # --- storage (git repo) -------------------------------------------------
    # Relative path within the skill repo, e.g. "public/tavily_ezhgmio_Tavily".
    storage_path: Mapped[str | None] = mapped_column(String(512))
    # Latest git commit sha that stored this skill.
    repo_commit: Mapped[str | None] = mapped_column(String(64))
    # Security-audit derived state: pass | suspect (danger is never stored).
    audit_state: Mapped[str | None] = mapped_column(String(16), index=True)
    # 描述**当前发布内容**的那条审计记录(``rp_ai_skill_audit_record.id``)。
    #
    # 收敛器需要"当前这一版的结论是哪一条"这个答案,而它没法从时间戳推:记录的 created_at 只精确
    # 到秒(MySQL DATETIME 默认如此),同一秒里落下的两条(上传完紧接着编辑一次)谁在后是不确定的,
    # 而 id 是随机 uuid,拿它做次级排序等于抛硬币。所以这里把答案记下来,而不是每次去猜。
    # 老数据是 NULL,收敛器对它们仍按时间倒序取最新一条。
    audit_ref: Mapped[str | None] = mapped_column(String(32))

    # --- 外部托管 ------------------------------------------------------------
    # 内容不在门户的仓库里,而在别人那儿。第一个来源是 IVC 的 Nexus,它对生产机房整段 TCP
    # 不可达(ICMP 通而 22/80/443/8081/8443 全部静默超时,且无代理),所以门户拿不到包体,只能
    # 登记目录并把下载转过去 —— 用户的浏览器在工位网段上是能访问它的。
    #
    # 为什么不复用 ``storage_path`` + ``repo_commit``:那两个字段的含义是"内容在我们的仓库里
    # 的哪个位置、哪一版",下载路径据此真的去读字节。往里塞一个外部 URL 会让"门户存了这份
    # 内容"这句话在一部分行上悄悄变成假的,而调用方无法分辨。
    #
    # 为空即普通技能。老数据全部为空,所以既有读法不受影响。
    external_url: Mapped[str | None] = mapped_column(String(1024))
    external_source: Mapped[str | None] = mapped_column(String(32))

    @property
    def externally_hosted(self) -> bool:
        """内容在别处,下载要转出去。"""
        return bool(self.external_url)

    @property
    def has_custom_image(self) -> bool:
        """作者传过自己的封面图吗。

        判据是 ``image_mime`` 而不是 ``image_blob``:后者是 deferred 的,读它会把字节拖过来,
        而这个属性会在每一行读模型上被求值一次。前端拿它决定是指向 ``/api/skills/{id}/image``
        还是用 ``image`` 那个部门默认图路径。
        """
        return bool(self.image_mime)

    @property
    def uploader_name(self) -> str | None:
        """The uploader's LDAP display name (from the linked user row).

        ``author`` stores the uploader's EID (identity); the human-readable
        display name lives on the user record and is resolved here for read
        models so the UI can show e.g. "Miao Zhang M".
        """
        up = self.uploader
        return up.display_name if up else None

    @property
    def creator_label(self) -> str | None:
        """The name to show as the skill's creator.

        For user uploads ``author`` holds the uploader's EID, so show the
        person's LDAP display name (falling back to the EID). For imported
        skills ``author`` is a real label (e.g. "Anthropic") that differs from
        the uploader EID, so show it verbatim.
        """
        if self.author and self.author != self.uploader_eid:
            return self.author
        return self.uploader_name or self.uploader_eid

    # Relationships.
    uploader: Mapped["RpAiPortal | None"] = relationship(
        back_populates="uploaded_skills",
        primaryjoin="RpAiSkill.uploader_eid == RpAiPortal.eid",
        viewonly=True,
    )
    cases: Mapped[list["RpAiSkillCase"]] = relationship(
        back_populates="skill", cascade="all, delete-orphan"
    )
    sub_skills: Mapped[list["RpAiSubSkill"]] = relationship(
        back_populates="skill", cascade="all, delete-orphan"
    )
    input_params: Mapped[list["RpAiInputParam"]] = relationship(
        back_populates="skill", cascade="all, delete-orphan"
    )
    reviews: Mapped[list["RpAiSkillReview"]] = relationship(
        back_populates="skill", cascade="all, delete-orphan"
    )
    favorited_by: Mapped[list["RpAiUserFavorite"]] = relationship(
        back_populates="skill", cascade="all, delete-orphan"
    )
    versions: Mapped[list["RpAiSkillVersion"]] = relationship(
        back_populates="skill",
        cascade="all, delete-orphan",
        order_by="RpAiSkillVersion.id",
    )
    # 压在线上这一版之外的、正在人工复核的更新(最多一份)。它存在不影响这一行的任何字段:
    # 这一行始终描述**已发布**的内容。见 models/pending_update.py。
    pending_update: Mapped["RpAiSkillPendingUpdate | None"] = relationship(
        back_populates="skill", cascade="all, delete-orphan", uselist=False
    )


class RpAiSkillCase(Base):
    __tablename__ = "rp_ai_skill_case"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    skill_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("rp_ai_skill.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    image: Mapped[str | None] = mapped_column(String(512))

    skill: Mapped["RpAiSkill"] = relationship(back_populates="cases")


class RpAiSubSkill(Base):
    __tablename__ = "rp_ai_sub_skill"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    skill_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("rp_ai_skill.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    badge: Mapped[str | None] = mapped_column(String(64))
    icon_color: Mapped[str | None] = mapped_column(String(32))
    icon_bg: Mapped[str | None] = mapped_column(String(32))

    skill: Mapped["RpAiSkill"] = relationship(back_populates="sub_skills")


class RpAiInputParam(Base):
    __tablename__ = "rp_ai_input_param"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    skill_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("rp_ai_skill.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str | None] = mapped_column(String(32))
    required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    skill: Mapped["RpAiSkill"] = relationship(back_populates="input_params")
