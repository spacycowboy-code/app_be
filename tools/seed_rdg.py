"""Seed R&D Guide data from the frontend hardcode into the database.

Usage (run once after migrate):
    python -m tools.seed_rdg

Idempotent: skips rows whose node_key already exists.
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import SessionLocal
from app.models.rdg import RpAiRdgNode, RpAiRdgToolMapping

# ── Stage definitions (mirrors rd-guide.component.ts) ──────────────────────

def _node(stage_key, node_key, label, sort_order, parent_id=None):
    return dict(stage_key=stage_key, node_key=node_key, label=label,
                node_type="node", sort_order=sort_order, parent_id=parent_id)

def _pg(stage_key, node_key, label, sort_order):
    return dict(stage_key=stage_key, node_key=node_key, label=label,
                node_type="parallel-group", sort_order=sort_order, parent_id=None)

def _cont(stage_key, node_key, label, sort_order):
    return dict(stage_key=stage_key, node_key=node_key, label=label,
                node_type="continuous", sort_order=sort_order, parent_id=None)


# Each entry: (parent_node_key_or_None, node_dict)
# We build a flat list; parallel-group children reference their parent by key.
# Format: (parent_key | None, node_dict)
RAW_NODES: list[tuple[str | None, dict]] = [
    # ── early-phase ──────────────────────────────────────────────────────────
    (None, _node("early-phase", "pcs",              "PCS",              0)),
    (None, _node("early-phase", "oa-scope",         "OA Scope",         1)),
    (None, _pg  ("early-phase", "pg-ep-1",          "",                 2)),
    ("pg-ep-1", _node("early-phase", "pre-otac-prepare", "Pre-OTAC Prepare", 0)),
    ("pg-ep-1", _node("early-phase", "lmc-mdc-define",   "LMC/MDC Define",   1)),
    ("pg-ep-1", _node("early-phase", "oa-writing",        "OA Writing",       2)),
    (None, _pg  ("early-phase", "pg-ep-2",          "",                 3)),
    ("pg-ep-2", _node("early-phase", "pre-otac-review",  "Pre-OTAC Review",  0)),
    ("pg-ep-2", _node("early-phase", "oa-review",         "OA Review",        1)),

    # ── study ────────────────────────────────────────────────────────────────
    (None, _node("study", "ps-study",          "PS Study",          0)),
    (None, _pg  ("study", "pg-st-1",           "",                  1)),
    ("pg-st-1", _node("study", "solution-study",   "Solution Study",   0)),
    ("pg-st-1", _node("study", "otac-prepare",     "OTAC Prepare",     1)),
    (None, _node("study", "solution-proposal", "Solution Proposal", 2)),
    (None, _pg  ("study", "pg-st-2",           "",                  3)),
    ("pg-st-2", _node("study", "signal-path-review", "Signal Path Review", 0)),
    ("pg-st-2", _node("study", "iwd-review",          "IWD Review",         1)),
    ("pg-st-2", _node("study", "datasheet-review",    "Datasheet Review",   2)),
    (None, _node("study", "fs-fd-review",      "FS/FD Review",      4)),
    (None, _node("study", "solution-review",   "Solution Review",   5)),
    (None, _node("study", "sa-writing",        "SA Writing",        6)),
    (None, _node("study", "nds-writing",       "NDS Writing",       7)),
    (None, _pg  ("study", "pg-st-3",           "",                  8)),
    ("pg-st-3", _node("study", "sa-review",    "SA Review",    0)),
    ("pg-st-3", _node("study", "otac-review",  "OTAC Review",  1)),
    (None, _node("study", "nds-review",        "NDS Review",        9)),

    # ── development ──────────────────────────────────────────────────────────
    (None, _pg  ("development", "pg-dev-1", "", 0)),
    ("pg-dev-1", _node("development", "db-impl",    "DB Implementation",        0)),
    ("pg-dev-1", _node("development", "func-impl",  "Function Implementation",  1)),
    ("pg-dev-1", _node("development", "coli-impl",  "Coli Implementation",      2)),
    ("pg-dev-1", _node("development", "iface-impl", "Interface Implementation", 3)),
    (None, _node("development", "ut-impl",           "UT Implementation",   1)),
    (None, _node("development", "rsim-impl",         "RSIM Implementation", 2)),
    (None, _node("development", "code-review",       "Code Review",         3)),
    (None, _pg  ("development", "pg-dev-2",          "",                    4)),
    ("pg-dev-2", _node("development", "code-self-verify", "Code Self Verification", 0)),
    ("pg-dev-2", _node("development", "dvs-writing",      "DVS Writing",            1)),
    (None, _pg  ("development", "pg-dev-3",          "",                    5)),
    ("pg-dev-3", _node("development", "bug-analysis", "Bug Analysis", 0)),
    ("pg-dev-3", _node("development", "bug-fixing",   "Bug Fixing",   1)),
    (None, _node("development", "fix-verify",         "Fix Verify",          6)),
    (None, _node("development", "follow-your-commit", "Follow Your Commit",  7)),
    (None, _node("development", "backmapping",        "Backmapping",         8)),
    (None, _cont("development", "sdk-lmc-build",      "SDK/LMC Build",       9)),
    (None, _cont("development", "integration-pair",   "Integration Pair-work", 10)),
    (None, _cont("development", "design-test",        "Design Test",         11)),

    # ── test ─────────────────────────────────────────────────────────────────
    (None, _pg  ("test", "pg-tst-1", "", 0)),
    ("pg-tst-1", _node("test", "dtar-prepare",     "DTAR Prepare",          0)),
    ("pg-tst-1", _node("test", "coverage-analysis","Coverage Analysis",     1)),
    ("pg-tst-1", _node("test", "test-material",    "Test Material Prepare", 2)),
    ("pg-tst-1", _node("test", "vs-prepare",       "VS Prepare",            3)),
    (None, _pg  ("test", "pg-tst-2", "", 1)),
    ("pg-tst-2", _node("test", "env-prepare",      "Env Prepare",      0)),
    ("pg-tst-2", _node("test", "rsim-env-prepare", "RSIM Env Prepare", 1)),
    (None, _pg  ("test", "pg-tst-3", "", 2)),
    ("pg-tst-3", _node("test", "dvs-case-run",  "DVS Case Run",  0)),
    ("pg-tst-3", _node("test", "load-checker",  "Load Checker",  1)),
    (None, _node("test", "sst-coding",           "SST Coding",           3)),
    (None, _pg  ("test", "pg-tst-4", "", 4)),
    ("pg-tst-4", _node("test", "sst-case-run",  "SST Case Run",       0)),
    ("pg-tst-4", _node("test", "pdl-loop-run",  "PDL Loop Run",       1)),
    ("pg-tst-4", _node("test", "node-fv-run",   "Node FV Case Run",   2)),
    ("pg-tst-4", _node("test", "local-bdc",     "Local BDC",          3)),
    (None, _node("test", "failed-case-analysis", "Failed Case Analysis", 5)),
    (None, _node("test", "test-report",          "Test Report",          6)),
    (None, _node("test", "slip-analysis",        "Slip Analysis",        7)),

    # ── ci ───────────────────────────────────────────────────────────────────
    (None, _node("ci", "ci-env-uplift",     "Env Uplift",             0)),
    (None, _node("ci", "ci-script-uplift",  "Script Uplift",          1)),
    (None, _node("ci", "ci-handover-intro", "Handover Introduction",  2)),
    (None, _node("ci", "ci-support",        "CI Support",             3)),

    # ── driver ───────────────────────────────────────────────────────────────
    (None, _node("driver", "drv-make-plan",       "Make Plan",          0)),
    (None, _pg  ("driver", "pg-drv-1",            "",                   1)),
    ("pg-drv-1", _node("driver", "drv-resource-alloc", "Resource Allocation", 0)),
    ("pg-drv-1", _node("driver", "drv-manage-backlog", "Manage Backlog",      1)),
    (None, _cont("driver", "drv-routine-meeting", "Routine Meeting",    2)),
    (None, _cont("driver", "drv-routine-report",  "Routine Report",     3)),
    (None, _cont("driver", "drv-risk-mgmt",       "Risk Management",    4)),
    (None, _node("driver", "drv-ext-sync",        "External Team Sync Up", 5)),
    (None, _node("driver", "drv-document",        "Document",           6)),
    (None, _node("driver", "drv-f-decision",      "F Decision",         7)),
    (None, _pg  ("driver", "pg-drv-2",            "",                   8)),
    ("pg-drv-2", _node("driver", "drv-retro",           "Retro",           0)),
    ("pg-drv-2", _node("driver", "drv-release-prepare", "Release Prepare", 1)),

    # ── debug ────────────────────────────────────────────────────────────────
    (None, _node("debug", "dbg-ticket",      "Ticket Create",      0)),
    (None, _node("debug", "dbg-preliminary", "Preliminary Analysis", 1)),
    (None, _pg  ("debug", "pg-dbg-1",        "",                   2)),
    ("pg-dbg-1", _node("debug", "dbg-reproduce", "Reproduce", 0)),
    ("pg-dbg-1", _node("debug", "dbg-diagnose",  "Diagnose",  1)),
    (None, _pg  ("debug", "pg-dbg-2",        "",                   3)),
    ("pg-dbg-2", _node("debug", "dbg-fix",     "Fix",     0)),
    ("pg-dbg-2", _node("debug", "dbg-verify",  "Verify",  1)),
    ("pg-dbg-2", _node("debug", "dbg-deliver", "Deliver", 2)),
    (None, _pg  ("debug", "pg-dbg-3",        "",                   4)),
    ("pg-dbg-3", _node("debug", "dbg-doc",   "Doc",   0)),
    ("pg-dbg-3", _node("debug", "dbg-retro", "Retro", 1)),
]

# ── Tool mapping (mirrors TOOL_MAPPING in rd-guide.component.ts) ────────────
# Format: (tool_name, stages_list, activities_list)
TOOL_MAPPING_RAW = [
    ("COLI Assistant",          ["development"],                        ["coli-impl"]),
    ("Radio Log Analysis",      ["development", "test", "debug"],       ["bug-analysis", "dbg-preliminary", "dbg-diagnose"]),
    ("Knowledge Shards Hub",    ["debug", "study", "driver"],           ["solution-study", "drv-retro", "dbg-retro"]),
    ("Meeting Transcripter",    ["driver"],                             ["drv-routine-report", "drv-routine-meeting"]),
    ("DFE Copilot",             ["development", "test", "study"],       ["func-impl", "solution-study"]),
    ("SST Assistant",           ["test"],                               ["sst-case-run", "failed-case-analysis"]),
    ("Automated Test",          ["development", "test"],                ["dvs-case-run", "node-fv-run", "design-test"]),
    ("EzBridge",                ["development", "test", "debug"],       ["bug-analysis", "dbg-preliminary", "dbg-diagnose"]),
    ("TxChangeCycle Explorer",  ["study"],                              ["solution-study"]),
    ("kiro market",             ["development", "test"],                []),
    ("oneClick",                ["development", "test", "debug"],       ["code-self-verify", "bug-analysis", "failed-case-analysis", "sst-case-run", "design-test", "dbg-reproduce", "dbg-diagnose", "dbg-verify"]),
    ("int-test-assistant",      ["development"],                        ["sdk-lmc-build"]),
    ("radioAssist-vscode",      ["development", "test"],                ["sdk-lmc-build", "sst-case-run"]),
    ("kiro-jira",               ["development", "debug", "driver"],     ["dbg-ticket", "drv-manage-backlog"]),
    ("AC Onboarding Assistant", ["early-phase", "study", "development"], ["solution-study", "pcs"]),
    ("EVCC Domain Expert",      ["study", "development", "debug"],      ["bug-analysis", "dbg-preliminary", "dbg-diagnose"]),
    ("Radio SW Chatbot",        ["early-phase", "study", "development"], ["solution-study", "pcs"]),
    ("afv-analyzer",            ["test"],                               ["local-bdc", "node-fv-run"]),
    ("DP Slot Info Decode",     ["test", "development", "debug"],       ["bug-analysis", "dbg-preliminary", "dbg-diagnose"]),
]


def seed(db) -> None:
    # ── 1. Nodes ─────────────────────────────────────────────────────────────
    existing_keys = {r.node_key for r in db.query(RpAiRdgNode.node_key).all()}
    key_to_id: dict[str, int] = {
        r.node_key: r.id for r in db.query(RpAiRdgNode).all()
    }

    # Two-pass: first insert top-level (parent_key=None), then children.
    for parent_key, nd in RAW_NODES:
        if nd["node_key"] in existing_keys:
            continue
        parent_id = key_to_id.get(parent_key) if parent_key else None
        row = RpAiRdgNode(
            stage_key=nd["stage_key"],
            node_key=nd["node_key"],
            label=nd["label"],
            node_type=nd["node_type"],
            sort_order=nd["sort_order"],
            parent_id=parent_id,
        )
        db.add(row)
        db.flush()  # get row.id immediately
        key_to_id[nd["node_key"]] = row.id
        existing_keys.add(nd["node_key"])

    db.commit()
    print(f"  nodes: {len(key_to_id)} total in DB")

    # ── 2. Tool mappings ─────────────────────────────────────────────────────
    existing_mappings: set[tuple[str, str]] = {
        (r.node_key, r.tool_name)
        for r in db.query(RpAiRdgToolMapping.node_key, RpAiRdgToolMapping.tool_name).all()
    }

    rows_added = 0
    for tool_name, stages, activities in TOOL_MAPPING_RAW:
        for stage_id in stages:
            key = (f"stage:{stage_id}", tool_name)
            if key not in existing_mappings:
                db.add(RpAiRdgToolMapping(node_key=f"stage:{stage_id}", tool_name=tool_name))
                existing_mappings.add(key)
                rows_added += 1
        for activity_id in activities:
            key = (activity_id, tool_name)
            if key not in existing_mappings:
                db.add(RpAiRdgToolMapping(node_key=activity_id, tool_name=tool_name))
                existing_mappings.add(key)
                rows_added += 1

    db.commit()
    print(f"  tool_mappings: +{rows_added} rows added")


if __name__ == "__main__":
    with SessionLocal() as db:
        seed(db)
    print("Seed complete.")
