"""Official-skill import endpoint.

Called by the ``rp-ai-skill`` sync pipeline (GitLab CI) after it pushes updated
``official/`` skills, to ingest them into the marketplace database. Guarded by a
bearer token (``SKILL_IMPORT_TOKEN``); the endpoint is disabled when unset.
"""
import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.services.official_import import import_official_skills

router = APIRouter(prefix="/api/skills", tags=["skill-import"])
settings = get_settings()


@router.post("/import-official")
def import_official(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict:
    """Refresh the official skills from the repo and upsert them into the DB."""
    token = settings.skill_import_token
    if not token:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Import endpoint disabled (SKILL_IMPORT_TOKEN not configured)",
        )
    expected = f"Bearer {token}"
    # Constant-time comparison to avoid leaking the token via timing.
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing token")

    return import_official_skills(db)
