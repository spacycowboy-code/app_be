"""Lightweight, idempotent schema migration.

``create_all`` only creates missing tables; it does not add new columns to
existing ones. This helper adds the columns introduced for skill storage and
creates any new tables (e.g. rp_ai_skill_version) with the utf8mb4 charset.

Usage:
    python -m app.migrate
"""
from sqlalchemy import text

from app.database import Base, engine

import app.models  # noqa: F401  (register models on Base.metadata)

# Ensure new tables are created as utf8mb4.
for _t in Base.metadata.tables.values():
    _t.kwargs["mysql_charset"] = "utf8mb4"
    _t.kwargs["mysql_collate"] = "utf8mb4_unicode_ci"

# (table, column, DDL type) tuples to add if missing.
COLUMNS = [
    ("rp_ai_skill", "storage_path", "VARCHAR(512) NULL"),
    ("rp_ai_skill", "repo_commit", "VARCHAR(64) NULL"),
    ("rp_ai_skill", "audit_state", "VARCHAR(16) NULL"),
    # 当前发布的这一版对应的审计记录 id。见 models/skill.py:同一秒里的两条记录分不出先后,
    # 所以"哪条结论描述现在这一版"要记下来,不能靠排序猜。
    ("rp_ai_skill", "audit_ref", "VARCHAR(32) NULL"),
    # 作者自己上传的封面图(见 models/skill.py 里为什么存库而不是存宿主机目录)。
    # MEDIUMBLOB 而不是 BLOB:后者上限 64 KB,会把一张普通截图直接截断,而截断的图片在库里
    # 是"存住了"、在页面上是坏图 —— 这种失败最难查。上限 16 MB 远大于端点的 512 KB 闸门。
    ("rp_ai_skill", "image_blob", "MEDIUMBLOB NULL"),
    ("rp_ai_skill", "image_mime", "VARCHAR(64) NULL"),
    ("rp_ai_skill", "image_updated_at", "DATETIME NULL"),
    ("rp_ai_skill_audit_record", "judge_model", "VARCHAR(120) NULL"),
    ("rp_ai_skill_audit_record", "rules_hash", "VARCHAR(64) NULL"),
    ("rp_ai_skill_finding", "rule_id", "VARCHAR(120) NULL"),
    ("rp_ai_skill_finding", "aitech", "VARCHAR(32) NULL"),
    ("rp_ai_skill_finding", "remediation", "TEXT NULL"),
    ("rp_ai_skill_audit_record", "content_sha", "VARCHAR(64) NULL"),
    ("rp_ai_skill_audit_record", "reason", "VARCHAR(300) NULL"),
    ("rp_ai_skill_audit_record", "coverage", "VARCHAR(20) NULL"),
    ("rp_ai_skill_audit_record", "cause", "VARCHAR(40) NULL"),
    ("rp_ai_skill_audit_record", "scanner_version", "VARCHAR(120) NULL"),
    ("rp_ai_skill_audit_record", "policy_fingerprint", "VARCHAR(120) NULL"),
    ("rp_ai_skill_audit_record", "analyzers_used", "TEXT NULL"),
    ("rp_ai_skill_audit_record", "incomplete", "TEXT NULL"),
    # 这条可疑结论说的是"要顶掉线上那一版"的更新,还是技能本身。没有它,收敛器会把一次被拦下的
    # 更新当成"这个技能不该上线",于是下架一个已经审过的线上版本。老记录是 NULL(当时没有待复核
    # 更新这个概念),按 false 读。
    ("rp_ai_skill_audit_record", "replaces_published", "TINYINT(1) NULL"),
    ("rp_ai_skill_finding", "file", "VARCHAR(512) NULL"),
    ("rp_ai_skill_finding", "line", "INT NULL"),
    ("rp_ai_market_item", "last_activity", "VARCHAR(40) NULL"),
    ("rp_ai_market_item", "creator", "VARCHAR(64) NULL"),
    # Review gate for uploaded packages. DEFAULT 'approved' on purpose: the
    # column is new, every existing item was already in the market, and a
    # default of 'pending' would empty it on the next deploy.
    ("rp_ai_market_item", "status", "VARCHAR(16) NOT NULL DEFAULT 'approved'"),
    ("rp_ai_market_item", "reviewed_by", "VARCHAR(64) NULL"),
    ("rp_ai_market_item", "reviewed_at", "DATETIME NULL"),
    ("rp_ai_skill_download", "eid", "VARCHAR(64) NULL"),
    # Department snapshots for group-level reporting (see services/usage.py).
    ("rp_ai_skill_download", "department", "VARCHAR(64) NULL"),
    ("rp_ai_market_download", "department", "VARCHAR(64) NULL"),
    # How the row got here: 'login' (first sign-in) or 'import'. NOT NULL with a
    # default so the rows that predate the column need no backfill — every one of
    # them was created on a sign-in, which is the whole reason ever_signed_in
    # could read this table as "people who have used the portal".
    # See models/user.py::PORTAL_ROW_ORIGINS.
    ("rp_ai_portal", "origin", "VARCHAR(16) NOT NULL DEFAULT 'login'"),
    # Change detector for assets synced from a git tree: the sha of the asset's
    # own directory. Distinct from repo_commit / last_activity on purpose — see
    # the column comments in models/skill.py and models/market.py.
    ("rp_ai_skill", "content_sha", "VARCHAR(64) NULL"),
    ("rp_ai_market_item", "content_sha", "VARCHAR(64) NULL"),
    # 外部托管的技能:内容在别人的仓库里,门户只登记它并把下载转过去。第一个来源是 IVC 的
    # Nexus —— 它对生产机房整段 TCP 不可达(ICMP 通、22/80/443/8081/8443 全部静默超时、无代理
    # 配置),所以门户拿不到包体,只拿得到目录。``external_url`` 为空即普通技能,老数据全部为空,
    # 读法不变。
    ("rp_ai_skill", "external_url", "VARCHAR(1024) NULL"),
    ("rp_ai_skill", "external_source", "VARCHAR(32) NULL"),
]

# (table, column, 期望的 information_schema.column_type, MODIFY 用的类型声明) 四元组。
#
# 和上面的 COLUMNS 分开是因为两件事的判断依据不同:加列看"存不存在",改类型看"是不是已经是想要
# 的类型"。合成一张表就得为每一行记住该用哪种判断。
#
# 第三个字段是**小写的 column_type 原样**,不是 DDL —— MySQL 会把 `INT NOT NULL DEFAULT 0`
# 存成 column_type=`int`,两者对不上,所以要分别写。写错的表现是每次启动都白跑一次 ALTER,
# 不会坏数据但会在大表上很慢。
#
# 第四个字段**只写类型声明,不带列名**。列名由第二个字段提供、拼 SQL 时补上,所以整行里列名
# 只出现一次。上一版把列名也写进这里,而拼 SQL 的地方又拼了一遍,生成的是
# `MODIFY COLUMN downloads downloads INT ...` —— MySQL 1064,迁移每次启动都失败。
COLUMN_TYPES = [
    # downloads 建表时是 VARCHAR(32)。理由(前端显示 "5.6K")从未实现,而字符串让 SQL 层的
    # ORDER BY 变成字典序、让自增只能读-改-写。见 models/skill.py 里那段注释。
    # MySQL 会把 '43' 这样的值自动转成 43;线上核对过,52 行全是纯数字,没有需要人工处理的值。
    ("rp_ai_skill", "downloads", "int", "INT NOT NULL DEFAULT 0"),
]


def _column_exists(conn, table: str, column: str) -> bool:
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    ).scalar()
    return bool(row)


def _retype_sql(table: str, column: str, type_decl: str) -> str:
    """The ``ALTER`` that changes one column's type.

    Split out of the loop so the statement can be asserted on **without a MySQL
    server**: the retype pass only runs where ``information_schema`` answers, so
    on SQLite it is skipped and a malformed statement here reaches production
    without a single test going red. That is exactly how
    ``MODIFY COLUMN downloads downloads INT ...`` shipped.

    ``type_decl`` carries no column name — it is supplied here, once.
    """
    return f"ALTER TABLE {table} MODIFY COLUMN {column} {type_decl}"


def _column_type(conn, table: str, column: str) -> str | None:
    """当前的列类型(``information_schema.column_type``,例如 ``varchar(32)`` / ``int``)。

    列不存在时返回 ``None`` —— 那种情况归上面的"加列"那一趟管,不是这里。
    """
    row = conn.execute(
        text(
            "SELECT column_type FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    ).first()
    return None if row is None else str(row[0]).lower()


def main() -> None:
    # 1) Create any missing tables.
    Base.metadata.create_all(bind=engine)

    # 2) Ensure every department group has a category row to point at.
    #    Deliberately before the dialect check below: this is data, not DDL, so
    #    ``create_all`` does not cover it on any dialect. A group whose row is
    #    missing resolves to "no group" for every one of its members and the only
    #    symptom is a default that silently never fills in.
    from app.database import SessionLocal
    from app.services.dept_groups import ensure_group_categories

    with SessionLocal() as db:
        for slug in ensure_group_categories(db):
            print(f"  + created category row {slug!r}")

    # 3) Add missing columns to existing tables (MySQL-specific introspection).
    #    On other dialects (e.g. sqlite in staging/tests) create_all above already
    #    produced fresh tables with all columns, so skip the ALTER pass.
    if engine.dialect.name != "mysql":
        print(f"  (dialect={engine.dialect.name}) tables ensured; skipping column ALTERs")
        print("Migration complete.")
        return

    with engine.begin() as conn:
        for table, column, ddl in COLUMNS:
            if _column_exists(conn, table, column):
                print(f"  = {table}.{column} already present")
            else:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                print(f"  + added {table}.{column}")

    # 4) Change the type of columns whose declared type has moved on.
    #    ``create_all`` never touches an existing column, and the pass above only
    #    adds missing ones — so a column that was created with the wrong type stays
    #    wrong forever unless it is named here.
    with engine.begin() as conn:
        for table, column, want, type_decl in COLUMN_TYPES:
            have = _column_type(conn, table, column)
            if have is None:
                print(f"  ! {table}.{column} missing, cannot retype")
            elif have == want:
                print(f"  = {table}.{column} already {want}")
            else:
                conn.execute(text(_retype_sql(table, column, type_decl)))
                print(f"  ~ {table}.{column}: {have} -> {want}")
    print("Migration complete.")


if __name__ == "__main__":
    main()
