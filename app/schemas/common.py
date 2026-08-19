"""Shared Pydantic base configured to speak camelCase to the frontend."""
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, field_serializer


def to_camel(string: str) -> str:
    """Convert snake_case to camelCase (e.g. category_id -> categoryId)."""
    head, *tail = string.split("_")
    return head + "".join(word.capitalize() for word in tail)


def as_utc(value: object) -> object:
    """Label a naive DB timestamp with the DB's offset and return it as UTC.

    DB timestamps are stored naive in the DB server's local time zone. Emitting
    them unlabelled makes the browser read them as *its* local time, which is
    silently wrong by the offset between the two.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            # Imported lazily to avoid a circular import at module load.
            from app.database import db_utc_offset

            value = value.replace(tzinfo=timezone(db_utc_offset()))
        return value.astimezone(timezone.utc).isoformat()
    return value


class CamelModel(BaseModel):
    """Base model that serializes to camelCase but accepts either casing."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )

    @field_serializer(
        "created_at", "updated_at", "content_updated_at", check_fields=False
    )
    def _serialize_utc(self, value: object) -> object:
        """Emit timestamps as timezone-aware UTC ISO strings.

        Every timestamp the frontend compares must be labelled the same way: the
        Home feed decides "is this an update?" by comparing these strings, and a
        naive value sorts as *less than* an otherwise identical labelled one.
        """
        return as_utc(value)
