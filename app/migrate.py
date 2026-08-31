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
    # 外部托管的技能:内容在别人的仓库里,门户只登记它并把下载转过去。第一个来源是 IVC 的
    # Nexus —— 它对生产机房整段 TCP 不可达(ICMP 通、22/80/443/8081/8443 全部静默超时、无代理
    # 配置),所以门户拿不到包体,只拿得到目录。``external_url`` 为空即普通技能,老数据全部为空,
    # 读法不变。
    ("rp_ai_skill", "external_url", "VARCHAR(1024) NULL"),
    ("rp_ai_skill", "external_source", "VARCHAR(32) NULL"),
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


def main() -> None:
    # 1) Create any missing tables.
    Base.metadata.create_all(bind=engine)

    # 2) Add missing columns to existing tables (MySQL-specific introspection).
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
    print("Migration complete.")


if __name__ == "__main__":
    main()
