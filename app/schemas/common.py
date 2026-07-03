"""Shared Pydantic base configured to speak camelCase to the frontend."""
from pydantic import BaseModel, ConfigDict


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
