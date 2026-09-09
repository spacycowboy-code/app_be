"""Wording for the department report, in Chinese and English.

``lang=en`` renders the page **fully** in English: headings, KPI cards, table
headers, column names, metric names and units, and the long ``口径`` paragraphs
(``<p class="lead">``, ``.note``, ``.hint``, and each metric's ``note`` in
``figures.py``). An earlier round translated the skeleton only and left the prose
Chinese; that was deliberate at the time and has been superseded — a page that is
half one language reads as unfinished, and the prose is where the numbers' limits
are stated, which is exactly what a reader who cannot read Chinese needs.

The Chinese text lives here too rather than staying inline in ``render.py``. One
table with both languages side by side is what makes the two structural checks in
``tests/test_report_language.py`` possible: same key set, same placeholders per
key. Neither is expressible while one language is embedded in a template.

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

Percentages and dates arrive pre-formatted from ``render.py``. Format specs are
deliberately kept out of these strings: ``{pct:.1f}`` in one language and
``{pct:.0f}`` in the other is a difference nothing here would catch.
"""
from __future__ import annotations

#: The two values ``lang`` may take. Anything else is rejected at the endpoint
#: rather than quietly treated as Chinese: a typo in a bookmarked URL should be
#: visible, not silently answered with the other language.
LANGS = ("zh", "en")

DEFAULT_LANG = "zh"

#: Values containing ``{}`` are ``str.format`` templates and the placeholder names
#: are part of the contract — the test suite checks that both languages use the
#: same set of placeholders for the same key, because a dropped placeholder is a
#: ``KeyError`` at render time and an added one is a stray brace on the page.
_ZH: dict[str, str] = {
    "html_lang": "zh-CN",
    "title": "RP AI Portal 使用情况报告（按部门）",
    "h1": "RP AI Portal 使用情况报告",
    "sub": "范围 BNEW RTE RP · 按小部门与大部门（二级部门）分组 · 生成于 {now}"
           "（北京时间）<br>\n数据来自门户数据库 <span class=\"code\">vem-staging</span>；"
           "部门归属与部门人数来自\nMicrosoft Graph（<span class=\"code\">manager</span>"
           " / <span class=\"code\">directReports</span>），\n"
           "也就是 Teams 组织架构图的同一个数据源",
    # --- KPI cards -------------------------------------------------------
    "card_head": "RP 组织在职人数",
    "card_head_sub": "Graph 汇报树，{teams}",
    "card_inside": "其中用过门户",
    "card_inside_sub": "渗透率 {pct}",
    "card_active": "有过实际动作的人",
    "card_active_sub": "不只是登录，占 RP 内 {inside}的 {pct}",
    "card_zero": "零使用的小组",
    "card_zero_sub": "合计 {head}，无人登录过",
    "card_downloads": "skill 被取用（去重到人）",
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
    "col_contrib_taken_unit": "人数 / 取用数",
    "col_contrib_version_note": "不含首版",
    # --- activity columns (render.COLS) ----------------------------------
    "col_login_events": "近期登录",
    "col_skill_download": "下载 skill",
    "col_skill_upload": "上传 skill",
    "col_skill_update": "更新 skill",
    "col_agent_upload": "上传 agent",
    # --- 口径 prose ------------------------------------------------------
    "warn_disabled": "渗透率的分母含 <b>{disabled} 个已停用账号</b>（Teams 也算它们），\n"
                     "这批人登不进门户，所以<b>报告里的渗透率是偏保守的下限</b> ——\n"
                     "按能登录的 {enabled} 人算，全组织是\n{pct_enabled}，而不是\n{pct_all}。",
    "lead_cells": "每个活动格子里，左边是<b>人数</b>（做过这件事的不同的人），\n"
                  "斜杠后是<b>次数</b>。",
    "lead_used_vs_recent":
        "<b>「用过门户」和「近期登录」不是同一件事。</b>前者是<b>累计</b>至少登录过一次\n"
        "（{since_ever} 起，登录时建号，永久保留）；后者只统计登录明细表上线的\n"
        "{since_login} 之后 —— 也就是<b>还回来过的人</b>。\n"
        "RP 内 {inside}登录过，其中 {returning} 人之后回来过，\n"
        "另外 <b>{once} 人来过就没再出现</b>。核对过两个方向：登录明细里的人全都在"
        "「用过门户」里，\n反过来那 {once} 位建号时间无一例外都在 {since_login} 之前。",
    "lead_did_something":
        "<b>「有实际动作」不等于右边各列相加。</b>它统计的是<b>所有</b>登录以外的动作，\n"
        "而这张表只给其中一部分开了列 —— 收藏、评论、工具运行、市场取用都算在内，"
        "但没有单独的列。\nRP 内有 <b>{off_column}</b>只做过这些没有列的动作，"
        "所以横着加会比这一列少。",
    "hint_sort": "<b>点表头可排序。</b>数字列首次点击按从大到小，再点反向；\n"
                 "部门名列按字母序。活动列有四档循环：人数↓、人数↑、次数↓、次数↑ ——\n"
                 "人数看铺开了多少，次数看用得多深，这两个问题排出来的名次不一样，"
                 "同一张表都能看。\n底色条是同一列内的相对高度。"
                 "渗透率 ≥20% 绿色，&lt;20% 橙色，0 灰色。",
    "lead_teams_1": "小部门即 EID 直属的那一级，共 {teams}、{head}"
                    "（<b>包括一个人都没用过的</b>）。\n第二行灰字是它所属的大部门。",
    "lead_teams_2": "这张表<b>只列小组</b>，所以合计是 {teams_head} 而不是 {scope_real} ——\n"
                    "差的 {dept_level}是各部门负责人本人及直接向他汇报、"
                    "不属于任何小组的幕僚。\n他们不是团队，已计入第一节所属部门。",
    "lead_zero_1": "这 {teams}共 {head}，占 RP 组织的\n{pct}，"
                   "其中没有任何一个人登录过门户。\n"
                   "这是覆盖面上最大的一块空白，也是唯一一份能直接拿去做推广的名单。",
    "lead_zero_2": "这里说的是<b>小组</b>，不是二级部门 ——\n没有一个二级部门是完全没人用的。",
    "note_zero_leads":
        "另有 <b>{head}</b>也没登录过，但<b>没有列在上表里</b>：他们是\n"
        "{units} 这几个部门的<b>负责人本人及其直属幕僚</b>（不属于任何小组），\n"
        "<span class=\"code\">department</span> 字段只写到二级部门、没有下一级，"
        "所以按小部门分组时\n会各自成一行。他们已经计入所属二级部门（见第一节），"
        "这里不再单独当成「部门」列出 ——\n把负责人摆进小组名单会让这份名单没法用。\n"
        "要看这几位，请直接看第一节对应部门那一行。",
    "lead_outside": "这 {outside}来自其他组织，是自己找过来的，不在 RP 的推广范围内。\n"
                    "单独列出而不并进上面的表：他们所在组织有多少人、有没有被推广过，"
                    "这份报告查的是\n{scope} 的名册，答不上来。把他们塞进 RP 的分母，"
                    "算出的百分比既不描述 RP，\n也不描述他们的组织。",
    "lead_appendix": "每条写明它实际数的是什么、观察区间，以及有没有数不到的部分。\n"
                     "口径写在这里而不是脚注，因为这些数字将来会被引用，"
                     "引用的人需要知道它的边界。",
    "footer": "使用数据每次打开都重新查库，所以上面的数字就是此刻的。\n"
              "组织结构（谁在哪个部门、每个部门多少人）来自 Microsoft Graph 汇报树的快照，\n"
              "抓取于 {as_of}（北京时间），由 "
              "<span class=\"code\">tools/fetch_graph_tree.py</span> 单独刷新 ——\n"
              "生成这个页面本身不连任何目录服务。口径定义见\n"
              "<span class=\"code\">app/services/usage_report/</span>。",
    # --- section four (contributors) -------------------------------------
    "lead_people_1": "谁做了东西给别人用。默认按「skill 被多少人取用」排，点表头可改排序。",
    "lead_people_2": "「被取用」列左边是<b>取用过的人数</b>，右边是<b>取用数</b> ——\n"
                     "一个人取走一个 skill 算一次，他下载多少遍都还是一次。\n"
                     "两个都给，因为一个人拿走同一作者的三个 skill 是 <b>1 / 3</b>，"
                     "三个人各拿走一个也是 <b>3 / 3</b>，\n只看一列分不出来。\n"
                     "<b>右边这一列不是下载次数。</b>按次数算，一个反复下载的脚本就能把"
                     "任何人顶到名单最前面 ——\n库里最高的一条是同一个工号对同一个 skill "
                     "下载 93 次、相邻间隔中位数 1.0 秒，而那个 skill 正是他自己上传的。",
    "note_first_version":
        "<b>「更新版本」不含首次上传。</b>上传一个 skill 会同时写一条 1.0.0\n"
        "的版本记录，所以直接数版本记录会把「只上传过、从没改过」也算成一次更新 ——\n"
        "全库 {version_rows} 条版本记录里有 {first_versions}\n条是这样的首版，"
        "真正的更新只有 {real_updates} 次。\n"
        "这一列只数首版之后的版本，所以一个人「上传 3、更新 0」是正常的，\n"
        "意思是他上传的东西还没有再改过。",
    "note_not_ranking": "<b>这是贡献名单，不是排名。</b>「被取用」多少取决于做的是什么东西 ——\n"
                        "一个通用工具天然比一个专用脚本被更多人拿走，这不说明谁做得更好。",
    "note_non_dir": "<b>{names} 不是目录里的人。</b>它上传了 {skills} 个 skill、"
                    "被取用 {taken} 次（按人去重后的取用数）。"
                    "没有把它从表里删掉，因为它是真实的内容来源 ——"
                    "删掉会让人以为 skill 库比实际小。但它不该算进「多少人贡献过」。",
    # --- appendix definition list ----------------------------------------
    "defs_now": "当前：{people}{ev}。",
    "defs_unattributed": "另有 {count}无法归属到人：{note}",
    "defs_excluded": "另有 {count}{ev}，未计入。",
    #: ``，共 25 个 skill`` — appended to ``defs_now``.
    "ev_total": "，共 {count} {unit}",
    #: ``、共 7 次下载`` — appended inside ``defs_excluded``.
    "ev_and": "、共 {count} {unit}",
    #: Between enumerated names. A Chinese enumeration comma is wrong in English
    #: and an ASCII comma is wrong in Chinese, so it is a key rather than a
    #: literal at the join site.
    "list_sep": "、",
    #: Tooltip on an activity column header, set by the sort script.
    "js_sort_by_times": "按次数排序",
    "js_sort_by_people": "按人数排序",
    #: Shown where a person's team would go, when the directory has no such account.
    "not_in_directory": "目录里查不到这个账号",
    # --- 数字 + 量词 -----------------------------------------------------
    #: Read through ``Strings.count``. Two forms per key separated by ``|``;
    #: Chinese does not inflect, so both are the same. Per **site**, not per
    #: concept, because the surrounding sentence already supplies part of the
    #: phrase — sharing one noun everywhere would reword the Chinese.
    "cnt_people": "{n} 人|{n} 人",
    "cnt_people_bold": "{n} 人|{n} 人",
    "cnt_users": "{n} 名用户|{n} 名用户",
    "cnt_teams_small": "{n} 个小部门|{n} 个小部门",
    "cnt_teams_bare": "{n} 个|{n} 个",
    "cnt_teams_group": "{n} 个<b>小组</b>|{n} 个<b>小组</b>",
    "cnt_rows": "{n} 条|{n} 条",
    "cnt_accounts": "{n} 个非个人账号|{n} 个非个人账号",
}

_EN: dict[str, str] = {
    "html_lang": "en",
    "title": "RP AI Portal usage report (by department)",
    "h1": "RP AI Portal usage report",
    "sub": "Scope BNEW RTE RP · grouped by team and by department (second level) · "
           "generated {now} (Beijing time)<br>\n"
           "Usage data comes from the portal database "
           "<span class=\"code\">vem-staging</span>; department membership and head "
           "counts come from\nMicrosoft Graph (<span class=\"code\">manager</span> / "
           "<span class=\"code\">directReports</span>),\n"
           "the same source the Teams org chart is drawn from",
    # --- KPI cards -------------------------------------------------------
    "card_head": "People in RP",
    "card_head_sub": "Graph reporting tree, {teams}",
    "card_inside": "Of those, have used the portal",
    "card_inside_sub": "Adoption {pct}",
    "card_active": "People who did something",
    "card_active_sub": "More than signing in — {pct} of the {inside} in RP",
    "card_zero": "Teams with no usage",
    "card_zero_sub": "{head}, nobody has signed in",
    "card_downloads": "Skill downloads (unique per person)",
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
    "col_contrib_taken_unit": "people / take-ups",
    "col_contrib_version_note": "first version excluded",
    # --- activity columns (render.COLS) ----------------------------------
    "col_login_events": "Recent sign-ins",
    "col_skill_download": "Skill downloads",
    "col_skill_upload": "Skills uploaded",
    "col_skill_update": "Skill updates",
    "col_agent_upload": "Agents uploaded",
    # --- methodology prose -----------------------------------------------
    "warn_disabled": "The denominator includes <b>{disabled} disabled accounts</b> "
                     "(Teams counts them too),\nand those people cannot sign in at "
                     "all, so <b>every adoption figure here is a conservative "
                     "lower bound</b> —\nagainst the {enabled} people who can "
                     "actually sign in, the organisation as a whole is\n"
                     "{pct_enabled} rather than\n{pct_all}.",
    "lead_cells": "In each activity cell, the left-hand number is <b>people</b> "
                  "(distinct people who did it)\nand the number after the slash is "
                  "<b>times</b>.",
    "lead_used_vs_recent":
        "<b>\u201cUsed the portal\u201d and \u201crecent sign-ins\u201d are not the "
        "same thing.</b> The first is <b>cumulative</b>: signed in at least once\n"
        "(since {since_ever} — the account row is created on first sign-in and kept "
        "for good). The second only covers the period since the\nsign-in event table "
        "went live on {since_login} — in other words, <b>the people who came "
        "back</b>.\nInside RP {inside} has signed in, {returning} of them "
        "came back afterwards,\nand <b>{once} came once and were never seen "
        "again</b>. Checked both ways: everyone in the sign-in log also appears "
        "under \u201cused the portal\u201d,\nand conversely all {once} of those "
        "accounts were created before {since_login}, without exception."
        "|"
        "<b>\u201cUsed the portal\u201d and \u201crecent sign-ins\u201d are not the "
        "same thing.</b> The first is <b>cumulative</b>: signed in at least once\n"
        "(since {since_ever} — the account row is created on first sign-in and kept "
        "for good). The second only covers the period since the\nsign-in event table "
        "went live on {since_login} — in other words, <b>the people who came "
        "back</b>.\nInside RP {inside} have signed in, {returning} of them "
        "came back afterwards,\nand <b>{once} came once and were never seen "
        "again</b>. Checked both ways: everyone in the sign-in log also appears "
        "under \u201cused the portal\u201d,\nand conversely all {once} of those "
        "accounts were created before {since_login}, without exception.",
    "lead_did_something":
        "<b>\u201cDid something\u201d is not the sum of the columns to its right.</b> "
        "It counts <b>every</b> action other than signing in,\nwhile this table only "
        "gives some of them a column — favourites, comments, tool runs and market "
        "take-ups all count towards it\nbut have no column of their own. Inside RP "
        "<b>{off_column}</b> did nothing except those uncounted actions, so "
        "adding the row across comes out lower than this column.",
    "hint_sort": "<b>The headers sort.</b> A number column sorts high-to-low on the "
                 "first click and reverses on the second;\nthe department name "
                 "column sorts alphabetically. Activity columns cycle through four "
                 "settings: people↓, people↑, times↓, times↑ —\npeople shows how "
                 "far something spread, times shows how heavily it is used, the two "
                 "questions produce different rankings, and one table answers "
                 "both.\nThe shaded bar is relative height within its own column. "
                 "Adoption ≥20% is green, &lt;20% orange, 0 grey.",
    "lead_teams_1": "A team here is the level an EID reports into directly: "
                    "{teams}, {head}\n(<b>including the ones where "
                    "nobody has used it at all</b>). The grey second line is the "
                    "department the team belongs to.",
    "lead_teams_2": "This table <b>lists teams only</b>, so the total is "
                    "{teams_head} rather than {scope_real} —\nthe difference of "
                    "{dept_level} is the department heads themselves plus the "
                    "people reporting straight to them without belonging to any "
                    "team.\nThey are not a team, and they are already counted in "
                    "their department in section 1.",
    "lead_zero_1": "This {teams} holds {head}, "
                   "{pct} of RP,\nand not one of those people has ever signed in to "
                   "the portal.\nThis is the largest single gap in coverage, and the "
                   "only list here that can be taken straight to outreach."
                   "|"
                   "These {teams} hold {head} between them, "
                   "{pct} of RP,\nand not one of those people has ever signed in to "
                   "the portal.\nThis is the largest single gap in coverage, and the "
                   "only list here that can be taken straight to outreach.",
    "lead_zero_2": "This is about <b>teams</b>, not second-level departments —\n"
                   "there is no second-level department with no usage at all.",
    "note_zero_leads":
        "Another <b>{head}</b> has not signed in either, but is <b>not "
        "listed above</b>: they are a <b>department head, or somebody "
        "reporting straight to one</b>\nin {units} (belonging to no team). "
        "Their <span class=\"code\">department</span> field stops at the second "
        "level with nothing below it,\nso grouping by team puts them on a "
        "row of their own. They are already counted in their second-level "
        "department (see section 1)\nand are not listed separately as a "
        "\u201cdepartment\u201d here — putting a department head into a list of "
        "teams is what makes that list unusable.\nTo see them, read the "
        "corresponding department row in section 1."
        "|"
        "Another <b>{head}</b> have not signed in either, but are <b>not "
        "listed above</b>: they are the <b>department heads themselves and the "
        "people reporting straight to them</b>\nin {units} (belonging to no team). "
        "Their <span class=\"code\">department</span> field stops at the second "
        "level with nothing below it,\nso grouping by team puts each of them on a "
        "row of their own. They are already counted in their second-level "
        "department (see section 1)\nand are not listed separately as a "
        "\u201cdepartment\u201d here — putting a department head into a list of "
        "teams is what makes that list unusable.\nTo see these people, read the "
        "corresponding department row in section 1.",
    "lead_outside": "This {outside} is from another organisation and found "
                    "the portal on their own; they are outside RP's outreach.\n"
                    "Listed separately rather than folded into the tables above: "
                    "how many people their organisation has, and whether it has "
                    "ever been approached, are questions this report cannot answer "
                    "— it reads the {scope} roster.\nPutting them into RP's "
                    "denominator would produce a percentage that describes neither "
                    "RP nor their own organisation."
                    "|"
                    "These {outside} are from other organisations and found "
                    "the portal on their own; they are outside RP's outreach.\n"
                    "Listed separately rather than folded into the tables above: "
                    "how many people their organisation has, and whether it has "
                    "ever been approached, are questions this report cannot answer "
                    "— it reads the {scope} roster.\nPutting them into RP's "
                    "denominator would produce a percentage that describes neither "
                    "RP nor their own organisation.",
    "lead_appendix": "Each entry states what it actually counts, over what period, "
                     "and whether anything is missing from it.\nThe scope is stated "
                     "here rather than in a footnote because these numbers will be "
                     "quoted, and whoever quotes them needs to know their limits.",
    "footer": "Usage data is queried afresh every time this page is opened, so the "
              "numbers above are current as of now.\nThe org structure (who is in "
              "which department, and how many people each has) comes from a "
              "snapshot of the Microsoft Graph reporting tree\ntaken {as_of} "
              "(Beijing time) and refreshed separately by "
              "<span class=\"code\">tools/fetch_graph_tree.py</span> —\n"
              "rendering this page contacts no directory service itself. The "
              "definitions live in\n"
              "<span class=\"code\">app/services/usage_report/</span>.",
    # --- section four (contributors) -------------------------------------
    "lead_people_1": "Who made something for other people to use. Sorted by how "
                     "many people took each author's skills; the headers change "
                     "the sort.",
    "lead_people_2": "In the \u201ctaken\u201d column the left number is <b>how "
                     "many people</b> took these skills and the right one is <b>how "
                     "many take-ups</b>:\none person taking one skill counts once, "
                     "however many times they download it.\nBoth are given, because "
                     "one person taking three of an author\u2019s skills is "
                     "<b>1 / 3</b> while three people taking one each is "
                     "<b>3 / 3</b>,\nand neither column alone tells them apart.\n"
                     "<b>The right column is not a count of downloads.</b> Counting "
                     "downloads lets a script that re-downloads\nput anyone at the "
                     "top of the list — the highest in the database is one signum "
                     "downloading one skill\n93 times, a median 1.0 seconds apart, "
                     "and that skill was the one they had uploaded themselves.",
    "note_first_version":
        "<b>\u201cNew versions\u201d excludes the first upload.</b> Uploading a "
        "skill also writes a 1.0.0 version row,\nso counting version rows directly "
        "would score \u201cuploaded once, never touched again\u201d as an update —\n"
        "of the {version_rows} version rows in the database, {first_versions}\nare "
        "first versions of exactly that kind, and only {real_updates} are real "
        "updates.\nThis column counts only versions after the first, so "
        "\u201cuploaded 3, updated 0\u201d is normal:\nit means what that person "
        "uploaded has not been revised yet.",
    "note_not_ranking": "<b>This is a list of contributions, not a ranking.</b> "
                        "How often something is taken depends on what it is —\na "
                        "general-purpose tool is naturally taken by more people "
                        "than a single-purpose script, which says nothing about who "
                        "did the better work.",
    "note_non_dir": "<b>{names} is not a person in the directory.</b> It uploaded "
                    "{skills} skills, with {taken} take-ups (deduplicated per "
                    "person). It has not been removed "
                    "from the table, because it is a genuine source of content — "
                    "removing it would make the skill library look smaller than it "
                    "is. But it must not count towards \u201chow many people have "
                    "contributed\u201d.",
    # --- appendix definition list ----------------------------------------
    "defs_now": "Currently: {people}{ev}.",
    "defs_unattributed": "A further {count} cannot be attributed to anyone: "
                         "{note}",
    "defs_excluded": "A further {count}{ev}, not counted.",
    "ev_total": ", {count} {unit} in total",
    "ev_and": ", {count} {unit} in total",
    "list_sep": ", ",
    "js_sort_by_times": "Sort by times",
    "js_sort_by_people": "Sort by people",
    "not_in_directory": "not found in the directory",
    # --- number + noun, agreeing in number -------------------------------
    "cnt_people": "{n} person|{n} people",
    "cnt_people_bold": "{n} person|{n} people",
    "cnt_users": "{n} user|{n} users",
    "cnt_teams_small": "{n} team|{n} teams",
    "cnt_teams_bare": "{n} team|{n} teams",
    "cnt_teams_group": "{n} <b>team</b>|{n} <b>teams</b>",
    "cnt_rows": "{n} row|{n} rows",
    "cnt_accounts": "{n} non-personal account|{n} non-personal accounts",
}

#: English name and unit per metric key, for the appendix and the column
#: subtitles. Keyed by ``Metric.key`` rather than by the Chinese label so that
#: rewording the Chinese does not silently drop the translation.
#:
#: Two keys here were wrong on the first pass — ``market_upload`` and ``tool_run``
#: against the real ``market_item_upload`` and ``usage_event`` — so those two
#: appendix entries silently showed their Chinese names on the English page. The
#: fallback is what made it silent, so a test now asserts every key here is a real
#: metric key, which is the assertion that would have caught it.
_METRIC_EN: dict[str, tuple[str, str]] = {
    "ever_signed_in": ("Have signed in at least once", "people"),
    "login_events": ("Recent sign-ins", "sign-ins"),
    "skill_download": ("Skill downloads", "downloads"),
    "skill_upload": ("Skills uploaded", "skills"),
    "skill_update": ("Skill updates (new version)", "updates"),
    "skill_favorite": ("Skills favourited", "favourites"),
    "skill_comment": ("Skill comments", "comments"),
    "agent_use": ("Agents used (downloaded / taken)", "times taken"),
    "agent_upload": ("Agents uploaded", "agents"),
    "market_item_upload": (
        "Market items uploaded (agent / tool / platform, combined)",
        "market items",
    ),
    "market_favorite": ("Market items favourited", "favourites"),
    "usage_event": ("Tools opened / run (new telemetry)", "events"),
}

#: The English of each metric's ``note`` — the ``口径`` sentence shown under its
#: name in the appendix. Keyed by ``Metric.key`` for the same reason as
#: ``_METRIC_EN``.
#:
#: ``figures.py`` composes two of these from a shared scope sentence
#: (``_SCOPE_NOTE`` / ``_SCOPE_NOTE_MARKET``); the English is written out in full
#: per metric instead. Mirroring the concatenation would mean translating a
#: sentence fragment and trusting that it still reads correctly wherever it is
#: glued on, which is how a translation ends up ungrammatical in one place only.
_METRIC_NOTE_EN: dict[str, str] = {
    "ever_signed_in":
        "A row in the user table is created on first sign-in, so this is the "
        "cumulative \u201chow many people have ever used the portal\u201d — the "
        "widest definition here and the right one for coverage. It has no count of "
        "times, only one first sign-in per person. Accounts created by import are "
        "not included, because those people have never signed in.",
    "login_events":
        "The sign-in event table was added later and only covers the period since it "
        "went live. So this row is not \u201cpeople who have signed in\u201d — "
        "\u201cused the portal\u201d is already that number — but the people who "
        "came back after this table existed, which is to say the ones who stuck.",
    "skill_download":
        "The same person downloading the same skill more than once is counted each "
        "time, because a repeat download is itself evidence of repeated use.",
    "skill_upload":
        "Counted by the skill's uploader. Officially imported skills are written by "
        "a service account and are excluded, so everything here was submitted by a "
        "colleague. Scope: only what is currently published; taken down, still in "
        "review and rejected are all excluded (the first has been removed from the "
        "market, the others were never visible to anyone).",
    "skill_update":
        "Defined as \u201ca version row that is not the earliest one for its "
        "skill\u201d. The first upload does not count as an update, or the same "
        "event would be counted twice. Scope: only what is currently published; "
        "taken down, still in review and rejected are all excluded (the first has "
        "been removed from the market, the others were never visible to anyone).",
    "skill_favorite":
        "A favourite is the signal \u201cI will want this again\u201d, which says "
        "more about intent to return than a single download does.",
    "skill_comment":
        "The only trace of two-way interaction: somebody was willing to write "
        "something, rather than just taking the file away.",
    "agent_use":
        "Every market item taken so far is of type agent, so this row is agent "
        "usage. What the portal can observe as \u201cuse\u201d is taking the item or "
        "copying its install command; how many times the agent then runs locally is "
        "not something the portal sees.",
    "agent_upload":
        "Only agents submitted by a person. Items synced in from GitLab have no "
        "submitter, and crediting them to somebody would invent an author. Scope: "
        "still in review and rejected are excluded (they were never visible to "
        "anyone). Deleted market items are not here either — deleting one removes "
        "the row, and its downloads and favourites go with it.",
    "market_item_upload":
        "Agents, tools and AI platform entries counted together, because to the "
        "person submitting them it is the same action. Scope: still in review and "
        "rejected are excluded (they were never visible to anyone). Deleted market "
        "items are not here either — deleting one removes the row, and its "
        "downloads and favourites go with it.",
    "market_favorite": "",
    "usage_event":
        "This telemetry only went live on 2026-08-21 and is still in single digits, "
        "which supports no conclusion at all. It is listed so that the growth in the "
        "next report can be recognised as \u201cwe started recording\u201d rather "
        "than \u201cusage went up\u201d. Events belonging to deleted market items "
        "are not counted (the portal's own tool pages are not subject to this).",
}

#: The English of each metric's ``unattributed_note`` — why some of its rows carry
#: no person. Only the metrics that have one appear here.
_METRIC_UNATTR_EN: dict[str, str] = {
    "skill_download":
        "The download endpoint used to take the downloader as an optional query "
        "parameter, so a caller that omitted it left the row with nobody attached. "
        "These are real downloads that cannot be placed with anyone; they cluster "
        "between 2026-08-06 and 08-12 and are interleaved with records from the same "
        "period that do carry an EID. The endpoint now takes the authenticated "
        "caller, so no new ownerless rows are produced; these few cannot be "
        "recovered, because the identity was never recorded at the time and no other "
        "table covers that period.",
    "market_item_upload":
        "Items synced automatically from GitLab have no submitter. They are platform "
        "content, not one colleague's contribution.",
}


class Strings:
    """Wording for one language.

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

    def count(self, key: str, n: int) -> str:
        """``n`` with its noun, agreeing in number.

        English needs \u201c1 team\u201d against \u201c2 teams\u201d; Chinese does
        not inflect, so both halves of a Chinese entry are the same string. The two
        forms live in one value separated by ``|`` so that a noun cannot end up with
        a singular in one language and a plural in the other, and so the key-set and
        placeholder checks keep working unchanged.

        The nouns are per **site** rather than per concept — ``cnt_teams_bare``
        against ``cnt_teams_small`` — because the surrounding Chinese sentence
        already supplies part of the phrase, and sharing one noun across sites would
        change the Chinese wording.
        """
        forms = self._table[key].split("|")
        return forms[0 if n == 1 else -1].format(n=n)

    def pick(self, key: str, n: int, **kw: object) -> str:
        """A whole sentence, choosing the form that agrees with ``n``.

        Needed because agreement in English reaches past the noun to the verb:
        \u201c1 person **has** not signed in\u201d against \u201c2 people **have**
        \u201d. Getting the noun right and leaving the verb plural is what the first
        translated render did.

        A value with no ``|`` is used for any ``n``, which is how Chinese avoids
        carrying two identical copies of a long paragraph — two copies are two
        things to edit, and the second one gets forgotten.
        """
        forms = self._table[key].split("|")
        chosen = forms[0] if (n == 1 and len(forms) > 1) else forms[-1]
        return chosen.format(**kw)

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

    def metric_note(self, key: str, zh_note: str) -> str:
        """The metric's ``口径`` sentence.

        Falls back to the Chinese rather than to an empty string: an untranslated
        metric should read oddly, not lose the one paragraph that says what its
        number does and does not include.
        """
        if self.lang == "en" and key in _METRIC_NOTE_EN:
            return _METRIC_NOTE_EN[key]
        return zh_note

    def metric_unattr(self, key: str, zh_note: str) -> str:
        if self.lang == "en" and key in _METRIC_UNATTR_EN:
            return _METRIC_UNATTR_EN[key]
        return zh_note


def strings(lang: str | None) -> Strings:
    """``Strings`` for ``lang``, defaulting to Chinese when nothing was asked for."""
    return Strings(lang or DEFAULT_LANG)
