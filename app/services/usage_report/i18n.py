"""English wording for the department report's **skeleton**.

Scope, decided deliberately: headings, KPI card labels, table headers, column
names, metric names and units. The long ``口径`` paragraphs (``<p class="lead">``,
``.note``, ``.hint``, and each metric's ``note`` in ``figures.py``) stay Chinese in
both languages. So ``lang=en`` gives an English reader a page whose structure and
every number is readable, with the methodology prose still in Chinese.

Why a purpose-built table rather than gettext or Angular i18n: this is one
server-rendered page, and the report is **downloadable** — a downloaded file has
to be in one language, which rules out shipping both languages and toggling in
the browser. ``GET /api/department-report?lang=en`` renders one language; the
toggle lives in the dashboard toolbar because the report is served inside an
``<iframe srcdoc>`` sandboxed without ``allow-same-origin``, so a button inside
the page could not talk to the app around it.

Keys are looked up, never interpolated from data, and a missing key raises rather
than falling back — a silent fallback would put a Chinese string on the English
page where nobody would notice it.
"""
from __future__ import annotations

#: The two values ``lang`` may take. Anything else is rejected at the endpoint
#: rather than quietly treated as Chinese: a typo in a bookmarked URL should be
#: visible, not silently answered with the other language.
LANGS = ("zh", "en")

DEFAULT_LANG = "zh"

#: Skeleton wording. Values containing ``{}`` are ``str.format`` templates and the
#: placeholder names are part of the contract — the test suite checks that both
#: languages use the same set of placeholders for the same key, because a dropped
#: placeholder is a ``KeyError`` at render time and an added one is a stray brace
#: on the page.
_ZH: dict[str, str] = {
    "html_lang": "zh-CN",
    "title": "RP AI Portal 使用情况报告（按部门）",
    "h1": "RP AI Portal 使用情况报告",
    # --- KPI cards -------------------------------------------------------
    "card_head": "RP 组织在职人数",
    "card_head_sub": "Graph 汇报树，{teams} 个小部门",
    "card_inside": "其中用过门户",
    "card_inside_sub": "渗透率 {pct}",
    "card_active": "有过实际动作的人",
    "card_active_sub": "不只是登录，占 RP 内 {inside} 名用户的 {pct}",
    "card_zero": "零使用的小组",
    "card_zero_sub": "合计 {head} 人，无人登录过",
    "card_downloads": "skill 下载次数",
    "card_downloads_sub": "{since} 起",
    "card_outside": "RP 之外的用户",
    "card_outside_sub": "单独列在第五节",
    # --- chapter headings ------------------------------------------------
    "h2_big": "一、大部门（二级部门）对比",
    "h2_teams": "二、小部门明细",
    "h2_zero": "三、还没有人用过的小组",
    "h2_people": "四、贡献者：谁做了东西，谁的东西被用",
    "h2_outside": "五、RP 之外的用户",
    "h2_appendix": "附录　指标定义与口径",
    # --- table headers ---------------------------------------------------
    "th_big_dept": "大部门（二级部门）",
    "th_small_dept": "小部门",
    "th_real": "部门<br>实际人数",
    "th_real_win": "Graph 汇报树",
    "th_used": "用过<br>门户",
    "th_adoption": "渗透率",
    "th_active": "有实际<br>动作",
    "th_total": "合计",
    "th_name": "姓名",
    "th_team": "小组",
    "th_headcount": "人数",
    # ``人数 / 次下载`` — the second half is the metric's unit.
    "th_people_per": "人数 / {unit}",
    "th_people_only": "人数",
    "th_since": "{date} 起",
    # --- contributor table columns ---------------------------------------
    "col_contrib_upload": "上传 skill",
    "col_contrib_version": "更新版本",
    "col_contrib_market": "上传市场项",
    "col_contrib_taken": "skill 被取用",
    "col_contrib_taken_unit": "人数 / 次数",
    "col_contrib_version_note": "不含首版",
    # --- activity columns (render.COLS) ----------------------------------
    "col_login_events": "近期登录",
    "col_skill_download": "下载 skill",
    "col_skill_upload": "上传 skill",
    "col_skill_update": "更新 skill",
    "col_agent_upload": "上传 agent",
}

_EN: dict[str, str] = {
    "html_lang": "en",
    "title": "RP AI Portal usage report (by department)",
    "h1": "RP AI Portal usage report",
    # --- KPI cards -------------------------------------------------------
    "card_head": "People in RP",
    "card_head_sub": "Graph reporting tree, {teams} teams",
    "card_inside": "Of those, have used the portal",
    "card_inside_sub": "Adoption {pct}",
    "card_active": "People who did something",
    "card_active_sub": "More than signing in — {pct} of the {inside} users in RP",
    "card_zero": "Teams with no usage",
    "card_zero_sub": "{head} people, nobody has signed in",
    "card_downloads": "Skill downloads",
    "card_downloads_sub": "since {since}",
    "card_outside": "Users outside RP",
    "card_outside_sub": "listed separately in section 5",
    # --- chapter headings ------------------------------------------------
    "h2_big": "1. Departments (second level) compared",
    "h2_teams": "2. Teams in detail",
    "h2_zero": "3. Teams where nobody has used the portal",
    "h2_people": "4. Contributors: who made things, and whose things get used",
    "h2_outside": "5. Users outside RP",
    "h2_appendix": "Appendix　Metric definitions and scope",
    # --- table headers ---------------------------------------------------
    "th_big_dept": "Department (second level)",
    "th_small_dept": "Team",
    "th_real": "People<br>in unit",
    "th_real_win": "Graph reporting tree",
    "th_used": "Used<br>portal",
    "th_adoption": "Adoption",
    "th_active": "Did<br>something",
    "th_total": "Total",
    "th_name": "Name",
    "th_team": "Team",
    "th_headcount": "People",
    "th_people_per": "people / {unit}",
    "th_people_only": "people",
    "th_since": "since {date}",
    # --- contributor table columns ---------------------------------------
    "col_contrib_upload": "Skills uploaded",
    "col_contrib_version": "New versions",
    "col_contrib_market": "Market items",
    "col_contrib_taken": "Skills taken",
    "col_contrib_taken_unit": "people / times",
    "col_contrib_version_note": "first version excluded",
    # --- activity columns (render.COLS) ----------------------------------
    "col_login_events": "Recent sign-ins",
    "col_skill_download": "Skill downloads",
    "col_skill_upload": "Skills uploaded",
    "col_skill_update": "Skill updates",
    "col_agent_upload": "Agents uploaded",
}

#: English name and unit per metric key, for the appendix and the column
#: subtitles. Keyed by ``Metric.key`` rather than by the Chinese label so that
#: rewording the Chinese does not silently drop the translation. A key missing
#: here falls back to the Chinese label — that is on purpose for metrics nobody
#: has translated yet, and the test suite lists which ones those are so the gap
#: is visible instead of implied.
_METRIC_EN: dict[str, tuple[str, str]] = {
    "ever_signed_in": ("Have signed in at least once", "people"),
    "login_events": ("Recent sign-ins", "sign-ins"),
    "skill_download": ("Skill downloads", "downloads"),
    "skill_upload": ("Skills uploaded", "skills"),
    "skill_update": ("Skill updates (new version)", "updates"),
    "skill_favorite": ("Skills favourited", "favourites"),
    "skill_comment": ("Skill comments", "comments"),
    "agent_use": ("Agents used (taken from the market)", "times taken"),
    "agent_upload": ("Agents uploaded", "agents"),
    "market_upload": ("Market items uploaded (agent / tool / platform)", "items"),
    "market_favorite": ("Market items favourited", "favourites"),
    "tool_run": ("Tools opened / run (new telemetry)", "events"),
}


class Strings:
    """Skeleton wording for one language.

    ``__getitem__`` raises ``KeyError`` for an unknown key on purpose: the
    alternative is a page that silently shows the key name, or worse the other
    language, in a spot nobody looks at.
    """

    __slots__ = ("lang", "_table")

    def __init__(self, lang: str) -> None:
        if lang not in LANGS:
            raise ValueError(f"unknown lang {lang!r}, expected one of {LANGS}")
        self.lang = lang
        self._table = _EN if lang == "en" else _ZH

    def __getitem__(self, key: str) -> str:
        return self._table[key]

    def fmt(self, key: str, **kw: object) -> str:
        return self._table[key].format(**kw)

    @property
    def is_en(self) -> bool:
        return self.lang == "en"

    def metric_label(self, key: str, zh_label: str) -> str:
        """The metric's name, English when translated, else the Chinese one."""
        if self.lang == "en" and key in _METRIC_EN:
            return _METRIC_EN[key][0]
        return zh_label

    def metric_unit(self, key: str, zh_unit: str) -> str:
        if self.lang == "en" and key in _METRIC_EN:
            return _METRIC_EN[key][1]
        return zh_unit


def strings(lang: str | None) -> Strings:
    """``Strings`` for ``lang``, defaulting to Chinese when nothing was asked for."""
    return Strings(lang or DEFAULT_LANG)
