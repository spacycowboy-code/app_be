"""The three-part group shown on a market card.

Read-only and derived: it is computed from the author's LDAP department at
request time (see ``app/services/card_group.py``) and never accepted from a
client. Nobody uploading a skill gets to declare which office they sit in.
"""
from app.schemas.common import CamelModel


class CardGroupRead(CamelModel):
    """Where the author of an item sits: 地址 · 大部门 · 小部门."""

    # BJ | CD.
    site: str
    # Absent only for a person sitting at the Chengdu site's own level.
    big_dept: str | None = None
    # Absent for a flat Chengdu unit, which genuinely has no sub-unit.
    small_dept: str | None = None
    # The parts that exist, space-joined. Server-side so the order of the three
    # is decided once, rather than in each of the two card templates.
    label: str
    # Raw LDAP path, so a card can explain a three-letter code on hover instead
    # of leaving the reader to guess what "PPE" is.
    department: str
