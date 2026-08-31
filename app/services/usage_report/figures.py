"""Every per-person figure the database can support, and the window each covers.

The windows are the point of this module. The tables were not all created at the
same time — login events start 2026-08-17, market downloads 2026-08-14, skill
downloads 2026-08-06, skills themselves 2026-07-10 — so two metrics put side by
side are usually measuring different lengths of time. Read without that, the data
invites a false conclusion: "more people downloaded a skill than logged in" is
arithmetic on two different months, not a finding. Each metric therefore carries
its own observed range, taken from the data rather than written down by hand, and
the report prints it next to the number.

Two figures are also kept apart everywhere: how many *people* did something
(distinct EIDs) and how many *times* it happened. They answer different
questions — reach versus intensity — and a single "usage" number that blends them
lets one enthusiast look like a department.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Connection


@dataclass
class Metric:
    """One question asked of the data, with its answer per person."""

    key: str
    label: str
    #: What a single count means for one person, e.g. "skills uploaded".
    unit: str
    #: Why this is here / how to read it. Shown in the report.
    note: str
    per_eid: dict[str, int] = field(default_factory=dict)
    first_seen: str | None = None
    last_seen: str | None = None
    #: True when the per-person number is worth showing next to the head count:
    #: the same person uploading a second agent is a second agent, and a column
    #: that only counts people cannot show it.
    #:
    #: This used to read "False when the row *is* the thing (a skill, a market
    #: item) and counting events would be meaningless", and the upload metrics were
    #: all False on that reasoning. The effect was that a department which had
    #: uploaded three agents by one person showed ``1`` — the same as a department
    #: with one agent — and the reader had no way to tell. The prose "共 25 次上传"
    #: is indeed odd for items, but that is a wording problem, fixed by using each
    #: metric's own ``unit``; it is not a reason to drop the number.
    #:
    #: False is now for metrics where the count genuinely carries nothing beyond
    #: "this person did it": ever_signed_in (per person it is always 1) and the two
    #: favourite metrics, where the report cares about how many people keep things,
    #: not how long anyone's list is.
    countable: bool = True
    #: Rows that belong to this metric but carry no person, so they are real
    #: activity that cannot be placed in a department. Stated rather than
    #: dropped: a department total that quietly omits them reads as complete.
    unattributed: int = 0
    #: Why those rows have nobody attached.
    unattributed_note: str = ""
    #: What the excluded non-person accounts contributed, moved here by
    #: ``exclude`` rather than deleted. The whole report counts people, so a
    #: service account must not sit in the totals — but its output is still real
    #: (the official import really did add skills), and a reader comparing the
    #: report against the portal will see those rows and need them accounted for.
    excluded_people: int = 0
    excluded_events: int = 0
    #: Of the excluded, how many were real people held out on purpose (interns)
    #: rather than functional accounts. Kept apart because the report names the
    #: reason, and calling an intern a non-personal account is simply wrong.
    excluded_interns: int = 0
    excluded_intern_events: int = 0

    @property
    def people(self) -> int:
        return len(self.per_eid)

    @property
    def events(self) -> int:
        return sum(self.per_eid.values())

    def exclude(self, keep: set[str], interns: set[str] | None = None) -> None:
        """Move everyone outside ``keep`` into the excluded tally.

        Called once, after the person list is known, so that every figure in the
        report — cards, tables, definitions — is computed on the same population.
        Filtering at each use site instead is how the first version of this
        report ended up stating two different login counts.

        ``interns`` are separated out. They are excluded by choice, not because the
        account is not a person, and lumping them together made the report state
        "another twenty non-personal accounts" against a list of seventeen.
        """
        interns = interns or set()
        dropped = {e: n for e, n in self.per_eid.items() if e not in keep}
        if not dropped:
            return
        intern_part = {e: n for e, n in dropped.items() if e in interns}
        self.excluded_interns = len(intern_part)
        self.excluded_intern_events = sum(intern_part.values())
        self.excluded_people = len(dropped) - len(intern_part)
        self.excluded_events = sum(dropped.values()) - self.excluded_intern_events
        self.per_eid = {e: n for e, n in self.per_eid.items() if e in keep}


#: The statuses that mean "this thing was, at some point, really visible to other
#: people" — the population every contribution figure is counted over.
#:
#: The line drawn here is *was it ever on the market*, not *is it there now*.
#:
#: ``archived`` counts. A taken-down skill was live and downloadable first, and
#: ``skills.delete_skill`` keeps its row precisely because the portal facts hanging
#: off it — downloads, favourites, comments, audit records — cannot be recomputed
#: from the repository. Dropping it here would also contradict the download
#: columns, which go on counting the downloads a taken-down skill received: a
#: department could end up showing twelve downloads and nobody who ever uploaded
#: anything.
#:
#: ``pending`` and ``rejected`` do not count. The public list filters on
#: ``approved``, so neither was ever visible to anyone and neither can have been
#: downloaded — counting them would report an attempt as a contribution. For
#: ``rejected`` that is not merely untidy: ten packages that all failed review
#: would read as ten uploads, and that is a number someone can drive on purpose.
#:
#: The rule applies to the columns that count *things* — uploads, updates, market
#: items. It is deliberately not applied to the columns that count what a *person*
#: did: a download happened whatever later became of the thing downloaded, and
#: retracting it because the author took the skill down would report the author's
#: decision as the downloader's activity.
SKILL_COUNTED_STATUSES = ("approved", "archived")

#: Market items have no ``archived``: ``market.delete_item`` removes the row, and
#: the foreign keys cascade its downloads, favourites and versions away with it.
#: So a deleted agent is already absent from every query here and there is nothing
#: left to include — not a judgement that it should not count, but the reason it
#: cannot. Making market deletion keep its history is a separate change.
MARKET_COUNTED_STATUSES = ("approved",)


def _counted(table: str, statuses: tuple[str, ...]) -> str:
    """SQL predicate selecting the rows the report counts.

    Takes the table (or an alias) rather than assuming one, because the same rule
    has to be applied to bare-table queries, to aliased joins, and to the
    ``_range`` lookups that date each column's header. Dating the header without
    it would be its own small lie: the range would start at a submission that the
    column does not count.
    """
    return f"{table}.status in (" + ", ".join(f"'{s}'" for s in statuses) + ")"


_SKILL_COUNTED = _counted("rp_ai_skill", SKILL_COUNTED_STATUSES)
_MARKET_COUNTED = _counted("rp_ai_market_item", MARKET_COUNTED_STATUSES)

#: Same rule for the version table, which has no status of its own: a version row
#: is counted when the skill it belongs to is.
_VERSION_SKILL_COUNTED = (
    "exists (select 1 from rp_ai_skill s2 where s2.id = rp_ai_skill_version.skill_id "
    f"and {_counted('s2', SKILL_COUNTED_STATUSES)})"
)

#: Sentence appended to every metric whose population this rule narrows, so the
#: report states its own scope instead of leaving the reader to assume it.
_SCOPE_NOTE = (
    "口径：含已下架的（它曾经真的上线、被人取用过），"
    "不含还在复核里和被拒的（它们从未对任何人可见）。"
)

#: Market items get their own wording: they have no taken-down state to include,
#: because deleting one removes the row. Reusing the skill sentence here would
#: promise a population the table cannot contain.
_SCOPE_NOTE_MARKET = (
    "口径：不含还在复核里和被拒的（它们从未对任何人可见）。"
    "被删除的市场项不在这里 —— 删除会连行一起删掉，它的下载和收藏也一并没了。"
)


def _range(
    conn: Connection,
    table: str,
    column: str = "created_at",
    where: str | None = None,
) -> tuple[str | None, str | None]:
    """The first and last date a metric has data for.

    ``where`` narrows the rows, which the update metric needs: its table gains a row
    on every upload too, so the plain minimum dated the column at the first upload
    and the header read "updates, since 2026-07-10" when the first actual update
    happened weeks later.
    """
    clause = f" where {where}" if where else ""
    row = conn.execute(
        text(f"select min(`{column}`), max(`{column}`) from `{table}`{clause}")
    ).one()
    return (str(row[0])[:10] if row[0] else None, str(row[1])[:10] if row[1] else None)


def _tally(conn: Connection, sql: str) -> dict[str, int]:
    """eid -> count, normalised so casing and stray spaces cannot split a person."""
    out: dict[str, int] = {}
    for eid, n in conn.execute(text(sql)):
        if eid and eid.strip():
            out[eid.strip().upper()] = out.get(eid.strip().upper(), 0) + int(n)
    return out


def collect(conn: Connection) -> list[Metric]:
    """Build every metric. Order is the order the report shows them in."""
    metrics: list[Metric] = []

    def add(
        m: Metric,
        table: str,
        column: str = "created_at",
        where: str | None = None,
    ) -> None:
        m.first_seen, m.last_seen = _range(conn, table, column, where)
        metrics.append(m)

    # --- signing in ---------------------------------------------------------
    # Two different questions, and the difference is not a discrepancy.
    # rp_ai_portal gains a row the first time someone signs in (auth.py), so it
    # is the full history of who has ever used the portal. rp_ai_login_event was
    # added later and is the only source for *how often*.
    #
    # The origin filter is what keeps the first sentence true. A row can now also
    # be created by an import, to register work by someone who has never signed
    # in — see models/user.py. Those rows are deliberately still counted
    # everywhere else (build_people places their contributions in a department,
    # and the card group badge reads their department), because none of that is a
    # claim about signing in. This metric is, so this metric filters.
    add(
        Metric(
            "ever_signed_in",
            "曾登录过的人数",
            "首次登录",
            "用户表的行在首次登录时创建，所以这是「累计有多少人用过门户」，"
            "口径最长、最适合看覆盖面。它没有次数，只有每人一次的首登记录。"
            "导入登记的账号行不算在内——那些人没有登录过。",
            per_eid=_tally(
                conn, "select eid, 1 from rp_ai_portal where origin = 'login'"
            ),
            countable=False,
        ),
        "rp_ai_portal",
        where="origin = 'login'",
    )
    add(
        Metric(
            "login_events",
            "近期登录",
            "次登录",
            "登录事件表是后加的，只覆盖它上线之后。所以这一行数的不是「登录过的人」"
            "——「用过门户」已经是那个数了——而是这张表上线后还回来过的人，"
            "也就是留存下来的。",
            per_eid=_tally(conn, "select eid, count(*) from rp_ai_login_event group by eid"),
        ),
        "rp_ai_login_event",
    )

    # --- skills ------------------------------------------------------------
    add(
        Metric(
            "skill_download",
            "下载 skill",
            "次下载",
            "同一人多次下载同一 skill 会各记一次，因为重复下载本身说明在反复用。",
            per_eid=_tally(
                conn, "select eid, count(*) from rp_ai_skill_download group by eid"
            ),
            unattributed=int(
                conn.execute(
                    text(
                        "select count(*) from rp_ai_skill_download "
                        "where eid is null or trim(eid)=''"
                    )
                ).scalar()
                or 0
            ),
            unattributed_note=(
                "门户早期没有把下载者写进下载记录，这些行是真实下载但无人可归。"
                "最早的一条在 2026-08-06，之后的记录都带人。"
            ),
        ),
        "rp_ai_skill_download",
    )
    add(
        Metric(
            "skill_upload",
            "上传 skill",
            "个 skill",
            "按 skill 的上传者统计。官方导入的由服务账号写入，已排除，"
            "所以这里全是同事自己交的。" + _SCOPE_NOTE,
            per_eid=_tally(
                conn,
                "select uploader_eid, count(*) from rp_ai_skill "
                f"where uploader_eid is not null and {_SKILL_COUNTED} "
                "group by uploader_eid",
            ),
        ),
        "rp_ai_skill",
        where=_SKILL_COUNTED,
    )
    # An update is a version row that is not the first one for its skill. Ordered
    # by id rather than created_at: both sqlite and MySQL store DATETIME to the
    # second, so two versions published within the same second compare equal and
    # neither counts as later than the other — the update disappears. The id is
    # monotonic per insert and has no such tie.
    add(
        Metric(
            "skill_update",
            "更新 skill（发新版本）",
            "次更新",
            "定义为「不是该 skill 最早那一版的版本记录」。首次上传不计入更新，"
            "否则同一件事会被数两遍。" + _SCOPE_NOTE,
            per_eid=_tally(
                conn,
                f"""
                select v.uploader_eid, count(*)
                  from rp_ai_skill_version v
                  join (select skill_id, min(id) first_id
                          from rp_ai_skill_version group by skill_id) f
                    on f.skill_id = v.skill_id
                  join rp_ai_skill s on s.id = v.skill_id
                 where v.id > f.first_id
                   and v.uploader_eid is not null
                   and {_counted('s', SKILL_COUNTED_STATUSES)}
                 group by v.uploader_eid
                """,
            ),
        ),
        "rp_ai_skill_version",
        where=(
            "id > (select min(v2.id) from (select id, skill_id "
            "from rp_ai_skill_version) v2 "
            "where v2.skill_id = rp_ai_skill_version.skill_id)"
            f" and {_VERSION_SKILL_COUNTED}"
        ),
    )
    add(
        Metric(
            "skill_favorite",
            "收藏 skill",
            "个收藏",
            "收藏是「我以后还要用」的信号，比一次下载更能说明留存意图。",
            per_eid=_tally(
                conn, "select eid, count(*) from rp_ai_user_favorite group by eid"
            ),
            countable=False,
        ),
        "rp_ai_user_favorite",
    )
    add(
        Metric(
            "skill_comment",
            "评论 skill",
            "条评论",
            "唯一的双向互动痕迹：有人愿意写点什么，而不只是取走文件。",
            per_eid=_tally(
                conn,
                "select author_eid, count(*) from rp_ai_skill_comment "
                "where author_eid is not null group by author_eid",
            ),
        ),
        "rp_ai_skill_comment",
    )

    # --- agents / market ---------------------------------------------------
    # Every market download so far is of an item whose type is 'agent', so this
    # is literally "used an agent" rather than a proxy for it. The join is kept
    # anyway: the moment a tool gets downloaded, the number should stop counting
    # it as agent use on its own.
    add(
        Metric(
            "agent_use",
            "使用 agent（下载 / 取用）",
            "次取用",
            "市场里目前被取用的项全部是 agent 类型，所以这条就是 agent 使用量。"
            "门户能观测到的「使用」是取走或复制安装命令，agent 之后在本地跑多少次，"
            "门户看不到。",
            per_eid=_tally(
                conn,
                """
                select d.eid, count(*)
                  from rp_ai_market_download d
                  join rp_ai_market_item i on i.id = d.item_id
                 where i.item_type = 'agent'
                 group by d.eid
                """,
            ),
        ),
        "rp_ai_market_download",
    )
    add(
        Metric(
            "agent_upload",
            "上传 agent",
            "个 agent",
            "只统计人工提交的 agent。从 GitLab 同步进来的项没有提交人，"
            "把它们算作某人上传会凭空造出一个作者。" + _SCOPE_NOTE_MARKET,
            per_eid=_tally(
                conn,
                "select creator, count(*) from rp_ai_market_item "
                "where item_type = 'agent' and creator is not null "
                f"and {_MARKET_COUNTED} group by creator",
            ),
        ),
        "rp_ai_market_item",
        where=_MARKET_COUNTED,
    )
    add(
        Metric(
            "market_item_upload",
            "上传市场项（agent / tool / 平台，合计）",
            "个市场项",
            "把 agent、工具、AI 平台入口合在一起看，因为对提交者来说是同一个动作。"
            + _SCOPE_NOTE_MARKET,
            per_eid=_tally(
                conn,
                "select creator, count(*) from rp_ai_market_item "
                f"where creator is not null and {_MARKET_COUNTED} group by creator",
            ),
            unattributed=int(
                conn.execute(
                    text(
                        "select count(*) from rp_ai_market_item "
                        "where (creator is null or trim(creator)='') "
                        f"and {_MARKET_COUNTED}"
                    )
                ).scalar()
                or 0
            ),
            unattributed_note=(
                "从 GitLab 自动同步进来的项没有提交人。它们是平台内容，不是某位同事的贡献。"
            ),
        ),
        "rp_ai_market_item",
        where=_MARKET_COUNTED,
    )
    add(
        Metric(
            "market_favorite",
            "收藏市场项",
            "个收藏",
            "",
            per_eid=_tally(
                conn, "select eid, count(*) from rp_ai_market_favorite group by eid"
            ),
            countable=False,
        ),
        "rp_ai_market_favorite",
    )

    # --- newly instrumented ------------------------------------------------
    # Included for completeness and to set expectations, not because three rows
    # support a conclusion. Leaving it out would be worse: the next report will
    # show a jump here, and it should be visible that the jump is instrumentation
    # coming online rather than behaviour changing.
    add(
        Metric(
            "usage_event",
            "工具打开 / 运行（新埋点）",
            "次事件",
            "这套埋点 2026-08-21 才上线，目前只有个位数，不足以下任何结论。"
            "列出来是为了让下一份报告里的增长能被认出是「开始记录了」而不是「用量涨了」。",
            per_eid=_tally(
                conn, "select eid, count(*) from rp_ai_usage_event group by eid"
            ),
        ),
        "rp_ai_usage_event",
    )
    return metrics


def contributions(conn: Connection) -> tuple[dict[str, dict], dict[str, int]]:
    """Per-person contribution figures, and the gaps in attributing them.

    Returns one row per account that put something into the portal, plus the counts
    that could not be attributed to anybody.

    The asset side is kept apart from the consumption side deliberately. "Whose
    skill is used most" describes a contribution, in the same category as a list of
    commit authors. "Who downloads most" describes a person's own activity. They
    answer different questions and carry different weight, and blending them into
    one activity score would let the second ride on the legitimacy of the first.

    Both a count of take-ups and a count of distinct takers are returned, because
    one skill taken twenty times by one person and taken once by twenty people are
    not the same contribution, and the first number alone cannot tell them apart.

    ``rp_ai_market_item.creator`` is nullable and mostly null, so most market
    take-ups cannot be attributed to anyone. That count is returned rather than
    quietly dropped: a table showing a single person with a single take-up, out of
    thirty-two, would otherwise read as "almost nobody's agents get used" when the
    truth is "we do not know whose they are".
    """
    rows: dict[str, dict] = {}

    def row(eid: str) -> dict:
        return rows.setdefault(
            eid,
            {
                "skills": 0,
                "versions": 0,
                "market_items": 0,
                "skill_taken": 0,
                "skill_takers": 0,
                "market_taken": 0,
                "market_takers": 0,
            },
        )

    for eid, n in conn.execute(
        text(
            "select uploader_eid, count(*) from rp_ai_skill "
            f"where uploader_eid is not null and {_SKILL_COUNTED} "
            "group by uploader_eid"
        )
    ):
        row(str(eid).upper())["skills"] = n

    # Not every version row is an update. Uploading a skill writes a 1.0.0 row at the
    # same second the skill itself is created, so counting version rows outright would
    # report every uploader as having updated something they only ever uploaded once:
    # twenty-seven version rows across the portal, of which fifteen are those initial
    # ones and only twelve are real updates. Excluding the earliest row per skill is
    # safe here — every skill that has version rows at all has its first one written
    # at upload time, so nothing is being dropped that was actually an update.
    #
    # Ordering is by the autoincrement id, not by created_at: both sqlite and MySQL
    # store DATETIME to the second, and two versions saved inside the same second
    # are not greater than one another, which silently loses the update.
    for eid, n in conn.execute(
        text(
            "select v.uploader_eid, count(*) "
            "from rp_ai_skill_version v "
            "join (select skill_id, min(id) first_id "
            "        from rp_ai_skill_version group by skill_id) f "
            "  on f.skill_id = v.skill_id "
            "join rp_ai_skill s on s.id = v.skill_id "
            "where v.id > f.first_id and v.uploader_eid is not null "
            f"  and {_counted('s', SKILL_COUNTED_STATUSES)} "
            "group by v.uploader_eid"
        )
    ):
        row(str(eid).upper())["versions"] = n

    for eid, n in conn.execute(
        text(
            "select creator, count(*) from rp_ai_market_item "
            f"where creator is not null and {_MARKET_COUNTED} group by creator"
        )
    ):
        row(str(eid).upper())["market_items"] = n

    for eid, n, ppl in conn.execute(
        text(
            "select s.uploader_eid, count(*), count(distinct d.eid) "
            "from rp_ai_skill_download d join rp_ai_skill s on s.id = d.skill_id "
            f"where s.uploader_eid is not null and {_counted('s', SKILL_COUNTED_STATUSES)} "
            "group by s.uploader_eid"
        )
    ):
        r = row(str(eid).upper())
        r["skill_taken"], r["skill_takers"] = n, ppl

    for eid, n, ppl in conn.execute(
        text(
            "select m.creator, count(*), count(distinct d.eid) "
            "from rp_ai_market_download d join rp_ai_market_item m on m.id = d.item_id "
            f"where m.creator is not null and {_counted('m', MARKET_COUNTED_STATUSES)} "
            "group by m.creator"
        )
    ):
        r = row(str(eid).upper())
        r["market_taken"], r["market_takers"] = n, ppl

    one = lambda sql: conn.execute(text(sql)).scalar() or 0  # noqa: E731
    gaps = {
        "skills_without_uploader": one(
            f"select count(*) from rp_ai_skill where uploader_eid is null "
            f"and {_SKILL_COUNTED}"
        ),
        "market_items_without_creator": one(
            "select count(*) from rp_ai_market_item where creator is null "
            f"and {_MARKET_COUNTED}"
        ),
        # The download totals are NOT narrowed by status. They are a denominator over
        # real events, and dropping events from it to match the asset columns would
        # make the ratio state something neither population supports.
        "skill_downloads_unattributed": one(
            "select count(*) from rp_ai_skill_download d "
            "left join rp_ai_skill s on s.id = d.skill_id where s.uploader_eid is null"
        ),
        "market_downloads_unattributed": one(
            "select count(*) from rp_ai_market_download d "
            "left join rp_ai_market_item m on m.id = d.item_id where m.creator is null"
        ),
        "market_downloads_total": one("select count(*) from rp_ai_market_download"),
        "skill_downloads_total": one("select count(*) from rp_ai_skill_download"),
        # The three figures behind the "updates exclude the initial upload" rule, so
        # the report can state them instead of hardcoding numbers that go stale.
        # Narrowed by the same rule as the update column they explain: a rejected
        # skill's version rows would otherwise make the prose claim more updates than
        # the table shows.
        "version_rows_total": one(
            f"select count(*) from rp_ai_skill_version where {_VERSION_SKILL_COUNTED}"
        ),
        "first_versions": one(
            "select count(distinct skill_id) from rp_ai_skill_version "
            f"where {_VERSION_SKILL_COUNTED}"
        ),
        "real_updates": one(
            "select count(*) from rp_ai_skill_version v "
            "join (select skill_id, min(id) first_id "
            "        from rp_ai_skill_version group by skill_id) f "
            "  on f.skill_id = v.skill_id "
            "join rp_ai_skill s on s.id = v.skill_id "
            f"where v.id > f.first_id and {_counted('s', SKILL_COUNTED_STATUSES)}"
        ),
    }
    return rows, gaps
