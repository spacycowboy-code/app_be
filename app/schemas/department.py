"""Response shapes for the department detail pages."""
from __future__ import annotations

from app.schemas.common import CamelModel
from app.schemas.market import MarketItemRead
from app.schemas.skill import SkillSummary


class DepartmentMetrics(CamelModel):
    """The headline numbers above the cards.

    Counts only the three kinds the page renders. ``ai-platform`` items are
    deliberately absent from both the counts and the lists: counting a kind the
    page never shows would leave a number nothing on screen adds up to.
    """

    skill_count: int = 0
    agent_count: int = 0
    tool_count: int = 0


class DepartmentSummary(CamelModel):
    """One department, for the sidebar."""

    slug: str
    code: str
    label: str
    metrics: DepartmentMetrics


class DepartmentAssets(CamelModel):
    """One department's page: the numbers, then the three lists behind them."""

    slug: str
    code: str
    label: str
    metrics: DepartmentMetrics
    skills: list[SkillSummary] = []
    agents: list[MarketItemRead] = []
    tools: list[MarketItemRead] = []
