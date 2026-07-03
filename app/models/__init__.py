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
from app.models.user import RpAiPortal
from app.models.version import RpAiSkillVersion
from app.models.comment import RpAiSkillComment
from app.models.activity import RpAiActivity

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
    "RpAiSkillComment",
    "RpAiActivity",
]
