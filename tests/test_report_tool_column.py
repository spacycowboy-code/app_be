"""The department report counts tool uploads in their own column.

Before this, the two department tables (一、大部门（二级部门）对比 and 二、小部门明细)
had one market column, labelled 上传 agent, and it filtered on
``item_type = 'agent'``. So a department whose people had only ever submitted
tools read as zero — the number was right for its label and silently incomplete as
an answer to "what has this department produced". Tools are a separate market tab
and a separate GitLab group, so they get a separate column rather than being
folded into the agent one, which would have made the label a lie instead.

What these tests pin:

* the two columns do not bleed into each other — a tool is not counted as an
  agent and the reverse, which is the whole point of splitting them;
* both columns still exist, in both languages, because a missing ``col_*`` key is
  a ``KeyError`` at render time rather than a visibly blank header;
* the tool column's header is dated by tool rows only. Its ``_range`` used to be
  taken over the whole table, which for the agent column happens to be correct
  (the earliest market item is an agent) and for the tool column would read
  "since" a date nine months before the first tool existed.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.database import SessionLocal
from app.models.market import RpAiMarketItem
from app.services.usage_report import figures, render
from app.services.usage_report.i18n import _EN, _METRIC_EN, _ZH

AUTHOR = "ETOOLCOL"
PREFIX = "tool-column-probe"

#: The agent is deliberately the older row, mirroring production: the first market
#: item ever registered is an agent, so a range taken over the whole table dates
#: both columns from here.
AGENT_DAY = datetime(2026, 1, 2, 9, 0, 0)
TOOL_DAY = datetime(2026, 8, 3, 9, 0, 0)


def _item(suffix: str, item_type: str, created: datetime) -> RpAiMarketItem:
    return RpAiMarketItem(
        id=f"{PREFIX}-{suffix}",
        name=f"Tool Column Probe {suffix}",
        item_type=item_type,
        service_type="code",
        source="upload",
        creator=AUTHOR,
        status="approved",
        created_at=created,
        updated_at=created,
    )


@pytest.fixture()
def seeded():
    """Two agents and one tool by the same person, the agents dated earlier."""
    with SessionLocal() as db:
        db.add(_item("agent-1", "agent", AGENT_DAY))
        db.add(_item("agent-2", "agent", AGENT_DAY))
        db.add(_item("tool-1", "tool", TOOL_DAY))
        db.commit()
    yield
    with SessionLocal() as db:
        db.query(RpAiMarketItem).filter(
            RpAiMarketItem.id.like(f"{PREFIX}%")
        ).delete(synchronize_session=False)
        db.commit()


def _metrics() -> dict[str, figures.Metric]:
    with SessionLocal() as db:
        return {m.key: m for m in figures.collect(db.connection())}


def test_the_two_columns_do_not_count_each_other(seeded):
    """Three items by one person, split 2 / 1 — not 3 / 3 and not 3 / 0."""
    metrics = _metrics()
    assert metrics["agent_upload"].per_eid[AUTHOR] == 2
    assert metrics["tool_upload"].per_eid[AUTHOR] == 1


def test_the_combined_column_still_sees_all_three(seeded):
    """``market_item_upload`` is the contributor table's number and stays a total."""
    assert _metrics()["market_item_upload"].per_eid[AUTHOR] == 3


def test_both_upload_columns_are_shown(seeded):
    """Order matters only in that tool follows agent; presence is the assertion."""
    assert "agent_upload" in render.COLS
    assert "tool_upload" in render.COLS
    assert render.COLS.index("tool_upload") == render.COLS.index("agent_upload") + 1


@pytest.mark.parametrize("strings", [_ZH, _EN])
def test_every_column_has_a_label_in_both_languages(strings):
    """``render`` looks the header up as ``"col_" + key``; a gap is a KeyError."""
    missing = [k for k in render.COLS if f"col_{k}" not in strings]
    assert missing == []


def test_the_tool_column_is_also_named_in_the_english_appendix():
    """A metric absent from ``_METRIC_EN`` falls back to its Chinese name silently."""
    assert "tool_upload" in _METRIC_EN


def test_the_tool_column_is_dated_by_tools_only(seeded):
    """Otherwise the header reads "since <the first agent>", months too early.

    The agent column is checked in the same test because the fix narrowed its
    ``_range`` too: it was previously taken over the whole table and happened to
    agree, and an assertion is the only thing that keeps it agreeing on purpose.
    """
    metrics = _metrics()
    assert metrics["tool_upload"].first_seen == TOOL_DAY.date().isoformat()
    assert metrics["agent_upload"].first_seen == AGENT_DAY.date().isoformat()
