"""The ``ALTER`` the migration builds to change a column's type.

This has no MySQL in it on purpose. The retype pass only runs where
``information_schema`` answers, so on SQLite it is skipped entirely — which is
why a malformed statement reached production with the whole suite green. The
statement is therefore asserted as **text**, which every environment can do.

The bug being pinned: ``COLUMN_TYPES`` used to repeat the column name inside its
type declaration while the loop also inserted it, producing
``MODIFY COLUMN downloads downloads INT NOT NULL DEFAULT 0`` -> MySQL 1064. The
migration failed on every boot, the entrypoint read that as "database not ready"
and retried, and the column silently stayed ``varchar(32)`` while the model
declared it an integer.
"""
import re

from app.migrate import COLUMN_TYPES, _retype_sql


def test_the_statement_names_the_column_once():
    assert _retype_sql("rp_ai_skill", "downloads", "INT NOT NULL DEFAULT 0") == (
        "ALTER TABLE rp_ai_skill MODIFY COLUMN downloads INT NOT NULL DEFAULT 0"
    )


def test_no_entry_repeats_its_own_column_name_in_the_type_declaration():
    """The shape that broke production, asserted for every row of the table.

    A type declaration that starts with the column name means the generated SQL
    says it twice. Checking the table rather than one hand-written example is
    what makes this hold for rows added later.
    """
    for table, column, _want, type_decl in COLUMN_TYPES:
        first = type_decl.split()[0].strip("`")
        assert first.lower() != column.lower(), (
            f"{table}.{column}: 类型声明不该带列名，否则生成 "
            f"MODIFY COLUMN {column} {type_decl}"
        )


def test_every_generated_statement_mentions_its_column_exactly_once():
    for table, column, _want, type_decl in COLUMN_TYPES:
        sql = _retype_sql(table, column, type_decl)
        assert len(re.findall(rf"\b{re.escape(column)}\b", sql)) == 1, sql


def test_the_expected_type_is_a_column_type_not_a_declaration():
    """Field 3 is compared against ``information_schema.column_type``.

    MySQL stores ``INT NOT NULL DEFAULT 0`` as ``int``, so putting a whole
    declaration here would never match and the ALTER would run on every boot.
    """
    for table, column, want, _decl in COLUMN_TYPES:
        assert want == want.lower(), f"{table}.{column}: column_type 是小写的"
        assert " " not in want, (
            f"{table}.{column}: {want!r} 看着像声明而不是 column_type"
        )
