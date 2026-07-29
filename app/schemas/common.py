"""Shared Pydantic base configured to speak camelCase to the frontend."""
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, field_serializer


def to_camel(string: str) -> str:
    """Convert snake_case to camelCase (e.g. category_id -> categoryId)."""
    head, *tail = string.split("_")
    return head + "".join(word.capitalize() for word in tail)


class CamelModel(BaseModel):
    """Base model that serializes to camelCase but accepts either casing."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )

    @field_serializer("created_at", "updated_at", check_fields=False)
    def _serialize_utc(self, value: object) -> object:
        """Emit timestamps as timezone-aware UTC ISO strings.

        DB timestamps are stored naive in the DB server's local time zone; we
        label them with that offset and convert to UTC so the browser can
        render them in the viewer's local time (e.g. Beijing) correctly.
        """
        if isinstance(value, datetime):
            if value.tzinfo is None:
                # Imported lazily to avoid a circular import at module load.
                from app.database import db_utc_offset

                value = value.replace(tzinfo=timezone(db_utc_offset()))
            return value.astimezone(timezone.utc).isoformat()
        return value
