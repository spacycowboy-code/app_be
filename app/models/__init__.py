"""ORM models package.

Importing every model here ensures ``Base.metadata`` is fully populated
before ``create_all`` / migrations run.
"""
from app.models.category import RpAiCategory
from app.models.favorite import RpAiUserFavorite
from app.models.review import RpAiSkillReview
from app.models.skill import (
    RpAiInputParam,
    RpAiSkill,
    RpAiSkillCase,
    RpAiSubSkill,
)
from app.models.pending_update import RpAiSkillPendingUpdate
from app.models.user import RpAiPortal
from app.models.version import RpAiSkillVersion
from app.models.comment import RpAiSkillComment
from app.models.activity import RpAiActivity
from app.models.download import RpAiSkillDownload
from app.models.market import RpAiMarketItem
from app.models.market_version import RpAiMarketItemVersion
from app.models.market_favorite import RpAiMarketFavorite
from app.models.market_download import RpAiMarketDownload
from app.models.audit import RpAiSkillAuditRecord, RpAiSkillFinding
from app.models.login_event import RpAiLoginEvent
from app.models.usage_event import RpAiUsageEvent
from app.models.org_snapshot import RpAiOrgSnapshot  # noqa: F401
from app.models.inventory_report import RpAiInventoryReport  # noqa: F401

__all__ = [
    "RpAiPortal",
    "RpAiSkill",
    "RpAiSkillCase",
    "RpAiSubSkill",
    "RpAiInputParam",
    "RpAiCategory",
    "RpAiUserFavorite",
    "RpAiSkillReview",
    "RpAiSkillVersion",
    "RpAiSkillPendingUpdate",
    "RpAiSkillComment",
    "RpAiActivity",
    "RpAiSkillDownload",
    "RpAiMarketItem",
    "RpAiMarketItemVersion",
    "RpAiMarketFavorite",
    "RpAiMarketDownload",
    "RpAiSkillAuditRecord",
    "RpAiSkillFinding",
    "RpAiLoginEvent",
    "RpAiUsageEvent",
    "RpAiInventoryReport",
]
