"""Render the department usage report as one self-contained HTML page.

Ported from the offline script this report started as. What changed in the move:
the database connection is handed in rather than opened here, LDAP is not called
on the request path, and nothing is written to disk.

The usage figures are read live, so the page is never out of date with the
portal. The organisation itself — who reports to whom, and therefore every head
count and every percentage in the report — is read from a cached snapshot of the
Microsoft Graph reporting tree, the same graph the Teams org chart draws. That is
deliberate: departments change on the order of months, walking 945 people through
Graph takes minutes and a token this service does not hold, and a report that
silently fell back to a different definition of "department" would disagree with
the org chart the reader has open. Refreshed out of band by
``tools/fetch_graph_tree.py``; missing snapshot is an error, not a guess.
"""
from __future__ import annotations

import html
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.services.usage_report.departments import (
    NON_PERSON,
    UNKNOWN,
    Dept,
    parse,
    resolve,
)
from app.services.usage_report.figures import Metric, collect, contributions

#: The organisation this report is about.
SCOPE = "BNEW RTE RP"
# Signing in is not an activity. rp_ai_portal records that an account exists at all
# and rp_ai_login_event records visits, and a large minority of people in the portal
# have nothing but those: counting them under "did something" made the figure agree
# with the number of users and disagree with its own caption.
SIGNIN_KEYS = {"ever_signed_in", "login_events"}
SCOPE_KEY = "BNEW/RTE/RP/"


def short_unit(name: object) -> str:
    """Drop the organisation prefix every unit name in this report shares.

    The whole report is scoped to one organisation, so "BNEW RTE RP" in front of all
    seventy-odd unit names costs column width and tells the reader nothing. Three
    places were stripping it inline and the per-person tables were not, which put
    "BNEW RTE RP CD Radio Products Chengdu" under a name while the department tables
    two sections up said "CD Radio Products Chengdu".
    """
    text_ = str(name or "")
    return text_[len(SCOPE) + 1:] if text_.startswith(SCOPE + " ") else text_


# --------------------------------------------------------------------------
# people
# --------------------------------------------------------------------------
@dataclass
class Person:
    eid: str
    dept: Dept
    source: str


def build_people(
    conn: Connection, tree: dict, identities: dict[str, dict], bind_account: str
) -> tuple[dict[str, "Person"], dict[str, str], dict[str, int]]:
    """Split the portal's account rows into people and everything else.

    Three sources, in descending order of authority. Anybody on the reporting tree
    is a person and is placed by the tree. Otherwise an out-of-band identity
    snapshot decides, if one was supplied. Otherwise the account's own portal row
    decides: a name and a department both mean the directory answered for them at
    sign-in, since that is where ``_upsert_user`` copies those two fields from.

    The heuristic is the weakest of the three and is reported as such: it cannot
    tell a colleague who has never signed in from a test row, and it cannot tell
    this service's own directory bind account from a person, which is why that one
    is excluded by matching configuration rather than by name.
    """
    rows = list(conn.execute(text("select eid, display_name, department from rp_ai_portal")))
    on_tree = {
        str(rec.get("signum") or signum).strip().upper()
        for signum, rec in tree.get("people", {}).items()
    }
    bind = (bind_account or "").strip().upper()

    people: dict[str, Person] = {}
    excluded: dict[str, str] = {}
    provenance: dict[str, int] = defaultdict(int)
    for raw_eid, name, db_dept in rows:
        eid = (raw_eid or "").strip().upper()
        if not eid:
            continue
        known = identities.get(eid, {})
        if eid in on_tree:
            people[eid] = Person(eid, parse(db_dept), "org tree")
            provenance["组织树"] += 1
            continue
        if eid == bind:
            excluded[eid] = "这个服务自己的目录绑定账号"
            continue
        if known:
            is_person, dept, why = resolve(
                eid,
                db_dept,
                known.get("department"),
                known.get("employeeType"),
                bool(known.get("found")),
            )
            if not is_person:
                excluded[eid] = why
                continue
            people[eid] = Person(eid, dept, why)
            provenance["身份快照"] += 1
            continue
        if (name or "").strip() and (db_dept or "").strip():
            people[eid] = Person(eid, parse(db_dept), "portal row")
            provenance["门户账号行（推断）"] += 1
        else:
            excluded[eid] = "门户账号行里既没有姓名也没有部门，无法确认是个人"
    return people, excluded, dict(provenance)


# --------------------------------------------------------------------------
# aggregation
# --------------------------------------------------------------------------
@dataclass
class Cell:
    people: int = 0
    events: int = 0


@dataclass
class Group:
    key: str
    label: str
    sublabel: str = ""
    real: int = 0        # people in the department, per the directory
    users: int = 0       # of those, how many have used the portal
    active: int = 0      # of those, how many did anything beyond signing in
    cells: dict[str, Cell] = field(default_factory=dict)

    def cell(self, k: str) -> Cell:
        return self.cells.setdefault(k, Cell())

    @property
    def adoption(self) -> float:
        return self.users / self.real * 100 if self.real else 0.0


def org_attribution(
    tree: dict, skip: set[str], require_enabled: bool = True
) -> tuple[dict[str, Dept], dict[str, int], dict[str, int], dict, int]:
    """Where each person sits per the org chart, plus head counts to match.

    Returns the per-signum placement, the head count per big department, and the
    head count per small department.

    The org chart is the reference because it is what the reader has open. Teams
    groups by the reporting line, and grouping this report by the ``department``
    attribute instead produced units that differed from Teams by up to seven
    people — a report that cannot be reconciled against the tool on the reader's
    screen gets discarded whatever its numbers say.

    Three populations are handled differently, and each for a reason:

    * **In the tree with a consistent department** — placed by their branch, small
      unit from their own department string.
    * **In the tree with a stale department** — placed by their branch, small unit
      taken from their *manager's* department. Six people are in this state; HWD3
      is the former code for RD3 and the units it names no longer exist.
    * **Not in the tree at all** — thirteen people have no ``manager`` attribute, so
      no walk reaches them. They keep their own department, which is the right
      answer rather than a fallback: they do work in the unit it names.

    ``skip`` drops people from both the placement and the head counts. It carries
    the interns, who are excluded from this report throughout. Doing it here rather
    than subtracting a number afterwards keeps the denominator and the numerator
    describing the same population: a department's head count and its portal users
    are both built from what this function returns, so a person removed from one is
    removed from the other by construction. Subtracting a total at the end would
    leave interns counted among the users of the very departments whose size had
    just been reduced.
    """
    placement: dict[str, Dept] = {}
    big_head: dict[str, int] = defaultdict(int)
    small_head: dict[str, int] = defaultdict(int)
    # Human labels for units nobody has signed in from. Without these the seeded
    # rows fall back to their code path, and the one table meant to be handed to
    # somebody as a list of who to go and talk to reads "BNEW/RTE/RP/SWD/SW4".
    labels: dict[str, tuple[str, str]] = {}
    skipped = 0

    in_tree: set[str] = set()
    for signum, info in tree.get("people", {}).items():
        if info.get("employeeType") != "Workforce":
            continue
        # Membership of the reporting tree is what defines the population when the
        # tree comes from Graph, because that is how the org chart the reader has open
        # defines it — and it lists people whose account has already been switched
        # off. Filtering them out here would reproduce neither source.
        if require_enabled and not info.get("enabled"):
            continue
        if signum in skip:
            skipped += 1
            continue
        in_tree.add(signum)
        dept = parse(info.get("corrected") or info.get("department"))
        # The branch, not the parsed fourth code: for the six stale rows those
        # disagree, and the branch is the one the org chart shows.
        branch = str(info.get("branch") or UNKNOWN)
        dept = Dept(dept.small_key, dept.small_label, branch, branch, dept.org_prefix)
        placement[signum] = dept
        big_head[branch] += 1
        small_head[dept.small_key] += 1
        # Keep the longer spelling: one unit is written both "RD2 DIG Digital" and
        # "RD2 DIG Digital HW Design", and the fuller one is the better label.
        if len(dept.small_label) > len(labels.get(dept.small_key, ("", ""))[0]):
            labels[dept.small_key] = (dept.small_label, branch)

    return placement, dict(big_head), dict(small_head), labels, skipped


def aggregate(
    people: dict[str, Person],
    metrics: list[Metric],
    big_head: dict[str, int],
    small_head: dict[str, int],
    placement: dict[str, Dept],
    labels: dict[str, tuple[str, str]],
    level: str,
    in_scope: bool,
) -> dict[str, Group]:
    """Roll people and their counts up to one org level.

    Groups are seeded from the *directory*, not from the portal, so a unit where
    nobody has signed in still gets a row. Those rows are the ones worth acting on,
    and an aggregation built only from portal activity cannot produce them.

    Portal users are placed by the org chart where it knows them, so that numerator
    and denominator describe the same population. A user placed by their own stale
    ``department`` while the head count came from the reporting line would land in a
    unit whose denominator does not include them.
    """
    groups: dict[str, Group] = {}

    def keyof(d: Dept) -> tuple[str, str, str]:
        if level == "big":
            return d.big_key, d.big_label, ""
        return d.small_key, d.small_label, d.big_label

    if in_scope:
        heads = big_head if level == "big" else small_head
        for key, n in heads.items():
            if level == "big":
                label, sub = key, ""
            else:
                label, sub = labels.get(key, (key, ""))
            groups.setdefault(key, Group(key, label, sub)).real += n

    active_eids = {
        e for m in metrics if m.key not in SIGNIN_KEYS for e in m.per_eid
    }

    def dept_of(p: Person) -> Dept:
        # The org chart wins over the portal's stored department: it is what the
        # head count was built from, and for six people the two disagree.
        return placement.get(p.eid, p.dept)

    for p in people.values():
        d = dept_of(p)
        inside = p.eid in placement or d.small_key.startswith(SCOPE_KEY)
        if inside != in_scope:
            continue
        key, label, sub = keyof(d)
        g = groups.setdefault(key, Group(key, label, sub))
        if not g.sublabel and sub:
            g.sublabel = sub
        # Two spellings of one unit share a key; keep the more descriptive label.
        if len(label) > len(g.label):
            g.label = label
        g.users += 1
        if p.eid in active_eids:
            g.active += 1
    for m in metrics:
        for eid, n in m.per_eid.items():
            p = people.get(eid)
            if p is None:
                continue
            d = dept_of(p)
            inside = eid in placement or d.small_key.startswith(SCOPE_KEY)
            if inside != in_scope:
                continue
            key, _, _ = keyof(d)
            c = groups[key].cell(m.key)
            c.people += 1
            c.events += n
    return groups


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def esc(s: object) -> str:
    return html.escape(str(s if s is not None else ""))


def bar(value: int, peak: int) -> str:
    if peak <= 0 or not value:
        return ""
    return f'<span class="bar" style="width:{max(2.0, round(value / peak * 100, 1))}%"></span>'


#: Metrics with no column of their own. The agent take-up figures were dropped from
#: display because the market items they count mostly have no creator recorded, so a
#: take-up could not be attributed to anybody.
#:
#: They are still collected, and still count towards "did something beyond logging in":
#: taking an agent out of the market is real use, and for seven people it is the only
#: thing they have ever done. Dropping the metric outright had quietly written those
#: seven out of the head-line figure, which is a different claim from "we are not
#: showing this column".
HIDDEN_METRICS = {"agent_use"}

COLS = [
    ("login_events", "近期登录"),
    ("skill_download", "下载 skill"),
    ("skill_upload", "上传 skill"),
    ("skill_update", "更新 skill"),
    ("agent_upload", "上传 agent"),
]

CSS = """
:root{--ink:#242424;--muted:#767676;--line:#dcdcdc;--head:#f4f4f4;
      --accent:#1174e6;--warn:#a86500;--zero:#c4c4c4;--good:#1e7b45}
*{box-sizing:border-box}
body{margin:0;padding:30px 26px 70px;background:#fff;color:var(--ink);
     font:14px/1.55 -apple-system,"Segoe UI",Roboto,"Noto Sans CJK SC",
     "Microsoft YaHei",sans-serif}
.wrap{max-width:1240px;margin:0 auto}
h1{font-size:24px;font-weight:600;margin:0 0 4px}
.sec{padding-left:20px;border-left:2px solid var(--line);margin:0 0 30px}
.sec>summary{margin-left:-22px;cursor:pointer;list-style:none}
.sec>summary::-webkit-details-marker{display:none}
.sec>summary::marker{content:""}
.sec>summary h2{margin-bottom:6px}
.sec>summary h2::after{content:"▾";color:var(--muted);font-weight:400;
   font-size:11px;margin-left:9px;vertical-align:2px}
.sec:not([open])>summary h2::after{content:"▸"}
.sec>summary:hover h2::after{color:var(--accent)}
.sec>summary:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
@media(max-width:900px){.sec{padding-left:10px}.sec>summary{margin-left:-12px}}
/* Collapsed chapters must still print: a reader who folded one away to look at
   another should not silently lose it from the paper copy. */
@media print{.sec>summary h2::after{content:""}.sec:not([open])>*{display:revert}}
h2{font-size:17px;font-weight:600;margin:40px 0 6px;padding-top:14px;
   border-top:1px solid var(--line)}
h3{font-size:13.5px;font-weight:600;margin:24px 0 6px}
p{margin:6px 0}
.sub{color:var(--muted);font-size:12px;margin:0 0 20px}
.lead{color:var(--muted);font-size:12.5px;max-width:80ch;margin:0 0 10px}
table{border-collapse:collapse;width:100%;font-size:12.5px;margin:10px 0 4px}
th,td{border:1px solid var(--line);padding:5px 7px;text-align:right}
th{background:var(--head);font-weight:600;text-align:center;font-size:11px;
   line-height:1.28}
th.l,td.l{text-align:left}
tbody tr:hover{background:#f2f2f2}
td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
td.zero{color:var(--zero)}
tfoot td{background:var(--head);font-weight:600}
.barcell{position:relative;min-width:78px}
.bar{position:absolute;left:0;top:0;bottom:0;background:rgba(17,116,230,.13)}
.barcell span.v{position:relative}
.dept{font-weight:600}
.code{color:var(--muted);font-size:10.5px;font-family:ui-monospace,monospace}
.pct{font-weight:600}
.pct.hi{color:var(--good)}.pct.lo{color:var(--warn)}.pct.no{color:var(--zero)}
.cards{display:flex;flex-wrap:wrap;gap:9px;margin:14px 0 6px}
.card{border:1px solid var(--line);padding:9px 13px;min-width:142px;flex:1}
.card .n{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}
.card .k{font-size:11.5px;color:var(--muted)}
.card .w{font-size:10.5px;color:var(--muted);margin-top:3px}
.note{border-left:3px solid #9a9a9a;background:#f6f6f6;padding:9px 13px;
      margin:12px 0;font-size:12.5px}
.note.warn{border-left-color:var(--warn);background:#fdf9f2}
dl{margin:8px 0}dt{font-weight:600;font-size:12.5px;margin-top:10px}
dd{margin:1px 0 0;color:var(--muted);font-size:12px;max-width:90ch}
ul{margin:6px 0;padding-left:20px;font-size:12.5px}li{margin:4px 0}
.small{font-size:11px;color:var(--muted)}
.tag{display:inline-block;border:1px solid var(--line);padding:0 5px;
     font-size:10.5px;color:var(--muted);margin-left:5px}
.win{font-size:10px;color:var(--muted);font-weight:400;display:block}
table.sortable th{cursor:pointer;user-select:none;position:relative;
  padding-right:24px}
table.sortable th:hover{background:#e4e4e4}
table.sortable th::after{content:"\\2195";position:absolute;right:5px;top:50%;
  transform:translateY(-50%);font-size:15px;line-height:1;color:#8e8e8e;
  font-weight:700}
table.sortable th.asc::after{content:"\\2191";color:var(--accent);font-size:16px}
table.sortable th.desc::after{content:"\\2193";color:var(--accent);font-size:16px}
.hint{font-size:11px;color:var(--muted);margin:4px 0 0}
@media print{body{padding:0}.note{background:none}h2{page-break-after:avoid}
  table{page-break-inside:avoid}table.sortable th{cursor:auto}
  table.sortable th::after{content:""}}
"""

#: Click-to-sort, written out rather than pulled from a library: the report is a
#: single file that has to open from a filesystem path with no network, and one
#: comparator is not worth a dependency that would have to be vendored anyway.
#:
#: Sorting reads ``data-v`` instead of the cell's text. Cells show things like
#: "8 / 21" (people / events) and "13%", which sort wrongly as strings — "9"
#: after "10", and any percent sign making the whole value non-numeric.
#: ``data-v2`` holds the event count, so a second click on an activity column
#: sorts by intensity rather than reach; those are different questions and the
#: table can answer both without a second table.
SORT_JS = r"""
<script>
(function () {
  function val(cell, useSecond) {
    var raw = useSecond && cell.dataset.v2 !== undefined
      ? cell.dataset.v2 : cell.dataset.v;
    if (raw === undefined) raw = cell.textContent.trim();
    var n = parseFloat(raw);
    // Text columns (department names) fall through to a locale compare so that
    // sorting by name is still useful next to the numeric columns.
    return isNaN(n) || String(raw).trim() === '' ? String(raw) : n;
  }
  function sortTable(table, index, desc, useSecond) {
    var body = table.tBodies[0];
    var rows = Array.prototype.slice.call(body.rows);
    rows.sort(function (a, b) {
      var x = val(a.cells[index], useSecond), y = val(b.cells[index], useSecond);
      if (typeof x === 'number' && typeof y === 'number') {
        return desc ? y - x : x - y;
      }
      return desc ? String(y).localeCompare(String(x))
                  : String(x).localeCompare(String(y));
    });
    rows.forEach(function (r) { body.appendChild(r); });
  }
  document.querySelectorAll('table.sortable').forEach(function (table) {
    var heads = table.tHead.rows[0].cells;
    Array.prototype.forEach.call(heads, function (th, i) {
      var body = table.tBodies[0];
      var probe = body.rows[0] ? body.rows[0].cells[i] : null;
      var hasSecond = !!(probe && probe.dataset.v2 !== undefined
                         && probe.dataset.v2 !== probe.dataset.v);
      // An explicit state counter rather than reading the arrow class back:
      // deriving the next state from the current arrow needed three booleans and
      // still got the third click wrong.
      //
      //   plain column   : 0 primary desc -> 1 primary asc -> 0 ...
      //   activity column: 0 people desc  -> 1 people asc
      //                  -> 2 events desc -> 3 events asc -> 0 ...
      //
      // Both orders are worth having on an activity column: people answers "how
      // far did it spread", events answers "how hard is it used", and they rank
      // departments differently.
      var states = hasSecond ? 4 : 2;
      th.dataset.state = '-1';
      th.addEventListener('click', function () {
        var next = (parseInt(th.dataset.state, 10) + 1) % states;
        // Names read better A-Z; counts are almost always wanted biggest-first,
        // so a numeric column starts descending. Guessing right saves a click.
        if (i === 0 && next === 0) next = 1;
        th.dataset.state = String(next);
        var useSecond = next >= 2;
        var desc = next % 2 === 0;
        Array.prototype.forEach.call(heads, function (h) {
          h.classList.remove('asc', 'desc');
          if (h !== th) h.dataset.state = '-1';
        });
        th.classList.add(desc ? 'desc' : 'asc');
        th.title = useSecond ? '按次数排序' : '按人数排序';
        sortTable(table, i, desc, useSecond);
      });
    });
  });
})();
</script>
"""


def pct_cls(p: float, real: int) -> str:
    if not real:
        return "no"
    if p == 0:
        return "no"
    return "hi" if p >= 20 else "lo"


def person_table(rows: list[tuple], headers: list[tuple[str, str]]) -> str:
    """A sortable table of named people.

    ``rows`` are ``(label, sublabel, [(display, sort_value), ...])``. The sort value
    is carried separately from the display text for the same reason as the department
    tables: a cell reading "18 / 11" or a blank cell sorts wrongly as a string.
    """
    peaks = [
        max((r[2][i][1] for r in rows), default=0) or 1 for i in range(len(headers))
    ]
    body = []
    for label, sub, cells in rows:
        tds = []
        for i, (shown, value) in enumerate(cells):
            # ``barcell`` carries ``position:relative``. Without it the bar, which is
            # absolutely positioned, resolves against the nearest positioned
            # ancestor instead of its own cell and paints across the whole page.
            tds.append(
                f'<td class="num barcell" data-v="{value}">'
                f'{bar(value, peaks[i])}<span class="v">{shown}</span></td>'
            )
        subline = f'<div class="code">{esc(sub)}</div>' if sub else ""
        body.append(
            f'<tr><td class="l" data-v="{esc(label)}"><b>{esc(label)}</b>'
            f"{subline}</td>{''.join(tds)}</tr>"
        )
    head_cells = []
    for title, note in headers:
        hint = f'<div class="small">{esc(note)}</div>' if note else ""
        head_cells.append(f"<th>{esc(title)}{hint}</th>")
    head = "".join(head_cells)
    return (
        '<table class="sortable"><thead><tr><th class="l">姓名</th>'
        f"{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
    )


def table(groups: dict[str, Group], by_key: dict[str, Metric], is_big: bool,
          show_real: bool) -> str:
    peaks = {k: max((g.cell(k).people for g in groups.values()), default=0)
             for k, _ in COLS}
    peak_real = max((g.real for g in groups.values()), default=0)
    peak_users = max((g.users for g in groups.values()), default=0)
    order = sorted(groups.values(), key=lambda g: (-(g.real or g.users), -g.users, g.label))

    rows = []
    for g in order:
        sub = (f'<div class="code">{esc(g.sublabel)}</div>'
               if (not is_big and g.sublabel) else "")
        # The whole report is scoped to one organisation, so repeating its name on
        # all 71 rows costs width without telling the reader anything.
        name = short_unit(g.label) if not is_big else g.label
        # data-v carries the value to sort on, so the client does not have to parse
        # display text. Cells like "8 / 21" would otherwise sort by the string.
        tds = [f'<td class="l" data-v="{esc(name)}">'
               f'<span class="dept">{esc(name)}</span>{sub}</td>']
        if show_real:
            tds.append(f'<td class="num barcell" data-v="{g.real}">'
                       f'{bar(g.real, peak_real)}'
                       f'<span class="v">{g.real or "—"}</span></td>')
        tds.append(f'<td class="num barcell" data-v="{g.users}">'
                   f'{bar(g.users, peak_users)}'
                   f'<span class="v">{g.users or "0"}</span></td>')
        if show_real:
            p = g.adoption
            tds.append(f'<td class="num" data-v="{p:.4f}">'
                       f'<span class="pct {pct_cls(p, g.real)}">{p:.0f}%</span></td>')
        tds.append(f'<td class="num{"" if g.active else " zero"}" '
                   f'data-v="{g.active}">{g.active}</td>')
        for k, _ in COLS:
            c = g.cell(k)
            m = by_key[k]
            inner = str(c.people)
            if m.countable and c.events and c.events != c.people:
                inner += f'<span class="small"> / {c.events}</span>'
            tds.append(f'<td class="num barcell{"" if c.people else " zero"}" '
                       f'data-v="{c.people}" data-v2="{c.events}">'
                       f'{bar(c.people, peaks[k])}<span class="v">{inner}</span></td>')
        rows.append("<tr>" + "".join(tds) + "</tr>")

    tot_real = sum(g.real for g in groups.values())
    tot_users = sum(g.users for g in groups.values())
    tot_active = sum(g.active for g in groups.values())
    foot = [f'<td class="l">合计</td>']
    if show_real:
        foot.append(f'<td class="num">{tot_real}</td>')
    foot.append(f'<td class="num">{tot_users}</td>')
    if show_real:
        foot.append('<td class="num">'
                    f'{tot_users / tot_real * 100:.0f}%</td>' if tot_real else "<td></td>")
    foot.append(f'<td class="num">{tot_active}</td>')
    for k, _ in COLS:
        m = by_key[k]
        p = sum(g.cell(k).people for g in groups.values())
        e = sum(g.cell(k).events for g in groups.values())
        v = str(p) + (f'<span class="small"> / {e}</span>'
                      if m.countable and e and e != p else "")
        foot.append(f'<td class="num">{v}</td>')

    head = [f'<th class="l">{"大部门（二级部门）" if is_big else "小部门"}</th>']
    if show_real:
        head.append("<th>部门<br>实际人数<span class='win'>Graph 汇报树</span></th>")
    head.append("<th>用过<br>门户</th>")
    if show_real:
        head.append("<th>渗透率</th>")
    head.append("<th>有实际<br>动作</th>")
    for k, label in COLS:
        head.append(f'<th>{esc(label)}<span class="win">'
                    f'{by_key[k].first_seen or "—"} 起</span></th>')
    return ('<table class="sortable"><thead><tr>' + "".join(head)
            + "</tr></thead><tbody>" + "".join(rows) + "</tbody><tfoot><tr>"
            + "".join(foot) + "</tr></tfoot></table>")


def render(people, excluded, excluded_active, metrics, big, small, out_big,
           provenance, big_head, tree,
           contrib, gaps, ids, placement,
           off_roster) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    def _units(d: dict, verb: str) -> str:
        """Turn {'PO': 6, 'QA': 3} into readable Chinese, built from the data.

        Written from the measurement rather than typed in, so the example cannot
        drift away from the numbers it is illustrating.
        """
        if not d:
            return f"没有人{verb}"
        parts = [f"{k} {v} 名" for k, v in sorted(d.items(), key=lambda kv: -kv[1])]
        total = sum(d.values())
        # The total is only worth printing when there is more than one unit to add up;
        # "CD 4 名离职（共 4 名）" says the same thing twice.
        tail = f"（共 {total} 名）" if len(parts) > 1 else ""
        return f"{'、'.join(parts)}{verb}{tail}"

    orphan_n = sum(
        1
        for info in tree.get("people", {}).values()
        if info.get("orphan")
        and info.get("employeeType") == "Workforce"
        and info.get("enabled")
    )
    tree_people = tree.get("people", {})
    # Disabled accounts per department. This is the usual reason a single department
    # disagrees with the org chart: the chart keeps people for a while after their
    # account is switched off, and this report does not count them because they
    # cannot sign in. Counted here so the reader can add them back per department
    # instead of being told the difference is unexplainable.
    disabled_by_branch: dict[str, int] = {}
    for _rec in tree_people.values():
        if _rec.get("employeeType") == "Workforce" and not _rec.get("enabled"):
            _b = str(_rec.get("branch") or "")
            disabled_by_branch[_b] = disabled_by_branch.get(_b, 0) + 1
    nine = ["CD", "IVC", "RD2", "RD1", "RD3", "SWD", "PS", "PO", "QA"]
    # The population every percentage in this report is taken against.
    scope_real = sum(big_head.values())
    by_key = {m.key: m for m in metrics}

    # ---- 具体到人 ----------------------------------------------------------
    def _name(eid: str) -> tuple[str, str]:
        info = ids.get(eid, {})
        if not info.get("found"):
            return eid, "目录里查不到这个账号"
        person = people.get(eid)
        unit = placement.get(eid) or (person.dept if person else None)
        return str(info.get("name") or eid), short_unit(unit.small_label if unit else "")

    contrib_rows = []
    for eid, r in contrib.items():
        if not (r["skills"] or r["market_items"] or r["versions"]):
            continue
        label, sub = _name(eid)
        taken = (
            f'{r["skill_takers"]}<span class="sl"> / {r["skill_taken"]}</span>'
            if r["skill_taken"]
            else "—"
        )
        contrib_rows.append(
            (
                label,
                sub,
                [
                    (str(r["skills"] or "—"), r["skills"]),
                    (str(r["versions"] or "—"), r["versions"]),
                    (str(r["market_items"] or "—"), r["market_items"]),
                    (taken, r["skill_takers"]),
                ],
            )
        )
    contrib_rows.sort(key=lambda x: (-x[2][3][1], -x[2][0][1], x[0]))

    active_eids = {
        e for m in metrics if m.key not in SIGNIN_KEYS for e in m.per_eid
    }
    # Counted over RP's own users only, to match the card beside it. Mixing the two
    # populations put 76 next to 146 and a percentage worked out against 154, so the
    # obvious division came out at 52% and the printed figure said 49%.
    inside_eids = {
        e
        for e, p_ in people.items()
        if e in placement or p_.dept.small_key.startswith(SCOPE_KEY)
    }
    active = len(active_eids & inside_eids)
    # "用过门户" is cumulative — a row is written the first time somebody signs in and
    # never removed — while the login-event table only exists from a later date. The
    # gap is not a discrepancy but the more interesting number: people who came once
    # and did not come back.
    returning = len(set(by_key["login_events"].per_eid) & inside_eids)
    once_only = len(inside_eids) - returning

    # "Did something" is a union over every non-sign-in metric, but the table shows a
    # column for only some of them, so the reader cannot add the row up. Deliberately
    # phrased without a total count of metrics: the definitions list below leaves out
    # the metric with no column, so any number quoted here would fail the same
    # cross-check it is meant to fix.
    _shown_act = {k for k, _ in COLS} - SIGNIN_KEYS
    _visible_actors = {
        e for m in metrics if m.key in _shown_act for e in m.per_eid
    }
    off_column = len((active_eids - _visible_actors) & inside_eids)
    active_all = len(active_eids & set(people))


    non_dir = [e for e in contrib if not ids.get(e, {}).get("found")]
    non_dir_note = ""
    if non_dir:
        non_dir_note = (
            f'<div class="note"><b>{"、".join(non_dir)} 不是目录里的人。</b>'
            f'它上传了 {sum(contrib[e]["skills"] for e in non_dir)} 个 skill、'
            f'被取用 {sum(contrib[e]["skill_taken"] for e in non_dir)} 次。'
            "没有把它从表里删掉，因为它是真实的内容来源 —— 删掉会让人以为 skill 库比"
            "实际小。但它不该算进「多少人贡献过」。</div>"
        )

    contrib_head = [
        ("上传 skill", ""),
        ("更新版本", "不含首版"),
        ("上传市场项", ""),
        ("skill 被取用", "人数 / 次数"),
    ]

    people_section = f"""
<h2>四、贡献者：谁做了东西，谁的东西被用</h2>

<p class="lead">谁做了东西给别人用。默认按「skill 被多少人取用」排，点表头可改排序。</p>



<p class="lead">「被取用」列左边是<b>取用的人数</b>，右边是<b>次数</b>。两个都给，
因为一个 skill 被一个人下载二十次、和被二十个人各下载一次，是完全不同的贡献，
只看次数分不出来。</p>

<div class="note"><b>「更新版本」不含首次上传。</b>上传一个 skill 会同时写一条 1.0.0
的版本记录，所以直接数版本记录会把「只上传过、从没改过」也算成一次更新 ——
全库 {gaps.get("version_rows_total", 0)} 条版本记录里有 {gaps.get("first_versions", 0)}
条是这样的首版，真正的更新只有 {gaps.get("real_updates", 0)} 次。
这一列只数首版之后的版本，所以一个人「上传 3、更新 0」是正常的，
意思是他上传的东西还没有再改过。</div>
{person_table(contrib_rows, contrib_head)}



{non_dir_note}

<div class="note"><b>这是贡献名单，不是排名。</b>「被取用」多少取决于做的是什么东西 ——
一个通用工具天然比一个专用脚本被更多人拿走，这不说明谁做得更好。</div>
"""

    inside = sum(g.users for g in small.values())
    outside = len(people) - inside

    # The one department whose figure differs from the org chart, and the person it
    # differs by. Checked against Teams by hand: the account exists in the directory,
    # which is why the API counts it, and the chart has not picked it up yet. Written
    # out from the data rather than typed in, so nobody has to remember to update the
    # sentence when the lag clears.
    QA_LAG_EID = "EFAAIXN"
    d_qa = big.get("QA").real if big.get("QA") else 0
    _lag = tree.get("people", {}).get(QA_LAG_EID, {})
    qa_lag_name = str(_lag.get("name") or QA_LAG_EID)
    if _lag.get("title"):
        qa_lag_name += f"（{_lag['title']}）"
    tree_as_of = str(tree.get("as_of") or "—").replace("T", " ")


    # How many rows in the small-department table are a department's own level. Stated
    # in the text so a reader counting the tagged rows can check it.

    # The head's own office shows up as a department-sized row and is not one of the
    # second-level departments, so the total in this table is larger than the nine-
    # department total anybody will be holding it against. Worked out from the root of
    # the org walk rather than written in, so it survives a change of post-holder.
    def is_dept_level(key: str) -> bool:
        """Is this row a department's own level rather than a team?

        A key of exactly four codes — ``BNEW/RTE/RP/RD3`` — is not a team. It is
        whoever's ``department`` stops at the second level: the head of the unit and
        anybody reporting straight to them without belonging to a group.
        """
        return len(key.split("/")) == 4

    # These people belong to their department and are already counted in it at the
    # big-department level. They are kept out of the outreach list because that list
    # is read as "teams nobody has reached yet", and a head of department sitting
    # alone in a row of one is neither a team nor a gap in coverage — it is one
    # person, listed under a name that looks like an organisation.
    zero_all = [g for g in small.values() if g.users == 0]
    # Section two lists teams only. The department-level rows are one person or a
    # handful sitting directly under the head, and next to a team of eighteen they read
    # as tiny departments with terrible adoption. They stay counted in section one,
    # where they belong to their department's figure.
    teams = {k: g for k, g in small.items() if not is_dept_level(k)}
    teams_head = sum(g.real for g in teams.values())
    dept_level_head = scope_real - teams_head

    zero_depts = [g for g in zero_all if not is_dept_level(g.key)]
    zero_head = sum(g.real for g in zero_depts)
    zero_leads = [g for g in zero_all if is_dept_level(g.key)]
    zero_lead_head = sum(g.real for g in zero_leads)
    zero_lead_units = "、".join(
        sorted(g.key.split("/")[-1] for g in zero_leads)
    )

    defs = []
    for m in metrics:
        if m.key in HIDDEN_METRICS:
            continue
        extra = ""
        if m.unattributed:
            extra += (f'<div class="small" style="color:var(--warn)">另有 '
                      f"{m.unattributed} 条无法归属到人：{esc(m.unattributed_note)}</div>")
        if m.excluded_people:
            ev = f"、共 {m.excluded_events} 次" if m.countable else ""
            extra += (f'<div class="small">另有 {m.excluded_people} 个非个人账号{ev}'
                      f"，未计入。</div>")
        ev = (f"，共 {m.events} {esc(m.unit)}"
              if m.countable and m.events != m.people else "")
        defs.append(f"<dt>{esc(m.label)}<span class='tag'>{esc(m.first_seen)} → "
                    f"{esc(m.last_seen)}</span></dt>"
                    f"<dd>{esc(m.note)}<br>当前：{m.people} 人{ev}。{extra}</dd>")

    zero_rows = "".join(
        f'<tr><td class="l"><span class="dept">'
        f'{esc(short_unit(g.label))}'
        f'</span> <span class="code">{esc(g.sublabel)}</span></td>'
        f'<td class="num">{g.real}</td></tr>'
        for g in sorted(zero_depts, key=lambda g: -g.real)
    )

    def indent_sections(doc: str) -> str:
        """Wrap each chapter in a container so its body sits under its heading.

        Done to the finished document rather than by threading tags through the
        template: the chapters are built from several f-strings and one of them
        (the per-person tables) is assembled elsewhere, so opening a tag in one
        place and closing it in another is how an unclosed div gets shipped.
        """
        marks = [m.start() for m in re.finditer(r"<h2[ >]", doc)]
        if not marks:
            return doc
        end = doc.rindex('<p class="small" style="margin-top:26px">')
        out = doc[: marks[0]]
        bounds = marks + [end]
        for i, start in enumerate(marks):
            chunk = doc[start : bounds[i + 1]]
            head_end = chunk.index("</h2>") + len("</h2>")
            # <details> rather than a click handler: folding keeps working with
            # JavaScript off, the keyboard reaches it without extra attributes, and
            # "open" is the default so nothing is hidden from a first-time reader.
            out += (
                '<details class="sec" open><summary>'
                + chunk[:head_end]
                + "</summary>"
                + chunk[head_end:]
                + "</details>\n"
            )
        return out + doc[end:]

    return indent_sections(f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RP AI Portal 使用情况报告（按部门）</title>
<style>{CSS}</style></head><body><div class="wrap">

<h1>RP AI Portal 使用情况报告</h1>
<p class="sub">范围 BNEW RTE RP · 按小部门与大部门（二级部门）分组 · 生成于 {now}<br>
数据来自门户数据库 <span class="code">vem-staging</span>；部门归属与部门人数来自
Microsoft Graph（<span class="code">manager</span> / <span class="code">directReports</span>），
也就是 Teams 组织架构图的同一个数据源</p>

<div class="note">渗透率的分母含 <b>{tree.get("disabled", 0)} 个已停用账号</b>（Teams 也算它们），
这批人登不进门户，所以<b>报告里的渗透率是偏保守的下限</b> ——
按能登录的 {scope_real - tree.get("disabled", 0)} 人算，全组织是
{inside / max(scope_real - tree.get("disabled", 0), 1) * 100:.1f}%，而不是
{inside / scope_real * 100:.1f}%。</div>

<div class="note warn"><b>已知的一处差异：QA。</b>本报告 QA
{d_qa} 人，Teams 显示 {d_qa - 1} 人，差的是
<b>{esc(qa_lag_name)}</b> —— 目录里已经建号，Teams 的人数还没更新。
新建账号进 Teams 组织图有延迟，所以两边偶尔差一两个人，看的是哪一天的快照。
本报告抓取于 {tree_as_of}。</div>

<div class="cards">
  <div class="card"><div class="n">{scope_real}</div><div class="k">RP 组织在职人数</div>
    <div class="w">Graph 汇报树，{len(small)} 个小部门</div></div>
  <div class="card"><div class="n">{inside}</div><div class="k">其中用过门户</div>
    <div class="w">渗透率 {inside / scope_real * 100:.1f}%</div></div>
  <div class="card"><div class="n">{active}</div><div class="k">有过实际动作的人</div>
    <div class="w">不只是登录，占 RP 内 {inside} 名用户的 {active / max(inside, 1) * 100:.0f}%</div></div>
  <div class="card"><div class="n">{len(zero_depts)}</div><div class="k">零使用的小组</div>
    <div class="w">合计 {zero_head} 人，无人登录过</div></div>
  <div class="card"><div class="n">{by_key['skill_download'].events}</div>
    <div class="k">skill 下载次数</div>
    <div class="w">{by_key['skill_download'].first_seen} 起</div></div>
  <div class="card"><div class="n">{outside}</div><div class="k">RP 之外的用户</div>
    <div class="w">单独列在第五节</div></div>
</div>

<h2>一、大部门（二级部门）对比</h2>
<p class="lead">每个活动格子里，左边是<b>人数</b>（做过这件事的不同的人），
斜杠后是<b>次数</b>。</p>
{table(big, by_key, True, True)}
<p class="lead"><b>「用过门户」和「近期登录」不是同一件事。</b>前者是<b>累计</b>至少登录过一次
（{by_key['ever_signed_in'].first_seen} 起，登录时建号，永久保留）；后者只统计登录明细表上线的
{by_key['login_events'].first_seen} 之后 —— 也就是<b>还回来过的人</b>。
RP 内 {len(inside_eids)} 人登录过，其中 {returning} 人之后回来过，
另外 <b>{once_only} 人来过就没再出现</b>。核对过两个方向：登录明细里的人全都在「用过门户」里，
反过来那 {once_only} 位建号时间无一例外都在 {by_key['login_events'].first_seen} 之前。</p>
<p class="lead"><b>「有实际动作」不等于右边各列相加。</b>它统计的是<b>所有</b>登录以外的动作，
而这张表只给其中一部分开了列 —— 收藏、评论、工具运行、市场取用都算在内，但没有单独的列。
RP 内有 <b>{off_column} 人</b>只做过这些没有列的动作，所以横着加会比这一列少。</p>
<p class="hint"><b>点表头可排序。</b>数字列首次点击按从大到小，再点反向；
部门名列按字母序。活动列有四档循环：人数↓、人数↑、次数↓、次数↑ ——
人数看铺开了多少，次数看用得多深，这两个问题排出来的名次不一样，同一张表都能看。
底色条是同一列内的相对高度。渗透率 ≥20% 绿色，&lt;20% 橙色，0 灰色。</p>

<h2>二、小部门明细</h2>
<p class="lead">小部门即 EID 直属的那一级，共 {len(teams)} 个、{teams_head} 人（<b>包括一个人都没用过的</b>）。
第二行灰字是它所属的大部门。</p>
<p class="lead">这张表<b>只列小组</b>，所以合计是 {teams_head} 而不是 {scope_real} ——
差的 {dept_level_head} 人是各部门负责人本人及直接向他汇报、不属于任何小组的幕僚。
他们不是团队，已计入第一节所属部门。</p>
{table(teams, by_key, False, True)}

<h2>三、还没有人用过的小组</h2>
<p class="lead">这 {len(zero_depts)} 个<b>小组</b>共 {zero_head} 人，占 RP 组织的
{zero_head / scope_real * 100:.0f}%，其中没有任何一个人登录过门户。
这是覆盖面上最大的一块空白，也是唯一一份能直接拿去做推广的名单。</p>
<p class="lead">这里说的是<b>小组</b>，不是二级部门 ——
没有一个二级部门是完全没人用的。</p>
<table style="max-width:640px"><thead><tr><th class="l">小组</th>
<th style="width:72px">人数</th></tr></thead>
<tbody>{zero_rows}</tbody></table>

<div class="note">另有 <b>{zero_lead_head} 人</b>也没登录过，但<b>没有列在上表里</b>：他们是
{zero_lead_units} 这几个部门的<b>负责人本人及其直属幕僚</b>（不属于任何小组），
<span class="code">department</span> 字段只写到二级部门、没有下一级，所以按小部门分组时
会各自成一行。他们已经计入所属二级部门（见第一节），这里不再单独当成「部门」列出 ——
把负责人摆进小组名单会让这份名单没法用。
要看这几位，请直接看第一节对应部门那一行。</div>

{people_section}

<h2>五、RP 之外的用户</h2>
<p class="lead">这 {outside} 人来自其他组织，是自己找过来的，不在 RP 的推广范围内。
单独列出而不并进上面的表：他们所在组织有多少人、有没有被推广过，这份报告查的是
{SCOPE} 的名册，答不上来。把他们塞进 RP 的分母，算出的百分比既不描述 RP，
也不描述他们的组织。</p>
{table(out_big, by_key, True, False)}

<h2>附录　指标定义与口径</h2>
<p class="lead">每条写明它实际数的是什么、观察区间，以及有没有数不到的部分。
口径写在这里而不是脚注，因为这些数字将来会被引用，引用的人需要知道它的边界。</p>
<dl>{''.join(defs)}</dl>

<p class="small" style="margin-top:26px">使用数据每次打开都重新查库，所以上面的数字就是此刻的。
组织结构（谁在哪个部门、每个部门多少人）来自 Microsoft Graph 汇报树的快照，
抓取于 {tree_as_of}，由 <span class="code">tools/fetch_graph_tree.py</span> 单独刷新 ——
生成这个页面本身不连任何目录服务。口径定义见
<span class="code">app/services/usage_report/</span>。</p>

</div>{SORT_JS}</body></html>
""")


class OrgSnapshotMissing(RuntimeError):
    """No org snapshot, so there is no head count and no percentage to report.

    Raised rather than falling back to anything. Two fallbacks were considered and
    both were rejected by measurement:

    Grouping by the portal's own ``department`` column — free text copied at
    sign-in, and only for people who have signed in, so it cannot say how many
    people a department has at all.

    Walking the reporting line over LDAP, which the service already has
    credentials for. Tried: it returns 961 people against Graph's 945, and differs
    per department by up to seven (PO 70 against 63). Graph is what the Teams org
    chart draws, and a report that cannot be reconciled against the chart the
    reader has open gets discarded whatever its numbers say. It also took 119
    seconds, which is not a request.
    """


def load_org_tree(conn: Connection, snapshot_path: Path | str | None) -> dict:
    """The org structure: from the database if it has been pushed, else from a file.

    Database first so a deployed service needs no volume and survives a redeploy.
    The file is kept for local work, where running the fetch tool and pointing at
    its output is quicker than uploading.
    """
    from app.services.usage_report import org_tree as org_tree_mod

    row = conn.execute(
        text(
            "select payload, captured_at from rp_ai_org_snapshot "
            "order by id desc limit 1"
        )
    ).first()
    if row is not None:
        import json

        captured = row[1]
        if isinstance(captured, str):
            captured = datetime.fromisoformat(captured)
        return org_tree_mod.build(json.loads(row[0]), captured)

    if snapshot_path:
        path = Path(snapshot_path)
        if path.is_file():
            return org_tree_mod.load(path)
    raise OrgSnapshotMissing(str(snapshot_path or ""))


def build_html(
    conn: Connection,
    org_snapshot: Path | str | None = None,
    identities_snapshot: Path | str | None = None,
    bind_account: str = "",
) -> str:
    """Render the whole report. Reads the database; writes nothing."""
    tree = load_org_tree(conn, org_snapshot)

    identities: dict[str, dict] = {}
    if identities_snapshot:
        path = Path(identities_snapshot)
        if path.is_file():
            import json

            identities = {
                str(k).strip().upper(): v
                for k, v in json.loads(path.read_text(encoding="utf-8")).items()
            }

    people, excluded, provenance = build_people(conn, tree, identities, bind_account)
    metrics = collect(conn)
    contrib, gaps = contributions(conn)

    # Names for everyone who could appear in a table, not just the people counted in
    # the denominator: the per-person tables have a row for anybody with a record,
    # and a row showing a bare signum is not something a reader can act on.
    #
    # ``found`` means "the directory has a person by this name", not "we have a name
    # to print". The portal fills display_name for its service accounts too, so
    # treating a name as proof of personhood dropped the note explaining that seven
    # of the skills in the contributor table were uploaded by a bot.
    ids: dict[str, dict] = {}
    for eid, name, dept in conn.execute(
        text("select eid, display_name, department from rp_ai_portal")
    ):
        key = (eid or "").strip().upper()
        if key:
            ids[key] = {
                "found": False,
                "name": (name or "").strip() or key,
                "department": dept,
                "source": "portal",
            }
    for eid, known in identities.items():
        entry = ids.setdefault(eid, {"name": eid})
        entry.update(
            {
                "found": bool(known.get("found")),
                "title": known.get("title"),
                "source": "snapshot",
            }
        )
        if known.get("name"):
            entry["name"] = known["name"]
        if known.get("department"):
            entry["department"] = known["department"]
    # The org tree wins: it is where the head counts come from, and one person named
    # two different ways in one report is one person the reader cannot follow.
    for signum, rec in tree.get("people", {}).items():
        ids.setdefault(signum, {}).update(
            {
                "found": True,
                "name": rec.get("name") or signum,
                "title": rec.get("title"),
                "department": rec.get("corrected") or rec.get("department"),
                "source": "graph",
            }
        )

    excluded_active = {
        eid: why
        for eid, why in excluded.items()
        if any(eid in m.per_eid for m in metrics)
    }
    # One population for every figure. Filtering at each use site instead is how an
    # earlier version of this report managed to state two different login counts.
    for metric in metrics:
        metric.exclude(set(people))

    placement, big_head, small_head, labels, off_roster = org_attribution(
        tree, set(), require_enabled=False
    )
    big = aggregate(
        people, metrics, big_head, small_head, placement, labels, "big", in_scope=True
    )
    small = aggregate(
        people, metrics, big_head, small_head, placement, labels, "small", in_scope=True
    )
    out_big = aggregate(
        people, metrics, {}, {}, placement, labels, "big", in_scope=False
    )
    return render(
        people,
        excluded,
        excluded_active,
        metrics,
        big,
        small,
        out_big,
        provenance,
        big_head,
        tree,
        contrib,
        gaps,
        ids,
        placement,
        off_roster,
    )
