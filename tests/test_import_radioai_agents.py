"""The radioai catalogue import: pin the decisions it encodes.

The script ran once, against production, on 2026-08-31. These tests are not here
to protect a feature — there is no feature — but because the rows it wrote are
now permanent, and the reasoning behind them has to be checkable against
something other than a chat log. Each test corresponds to a decision someone
made about this data.

The CSV is written inline rather than pointed at the real export: the export is
not in the repo, and a test that silently skips when a file is missing is worse
than no test.
"""
import csv
import io

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.market import RpAiMarketItem
from tools import import_radioai_agents as imp

HEADER = [
    "id", "name", "description", "category", "tags", "url", "status",
    "is_recommended", "author_eid", "version", "access_type", "use_cases",
    "access_meta", "created_at",
]

#: One row per decision. Real signums and real URLs, because the point is to pin
#: what the production run did.
ROWS = [
    # An ordinary SWD author.
    ["10", "Radio Log Analysis", "log tool", "Agent", "Test|Maintain",
     "https://radioai.ews.gic.ericsson.se/api/radio-log-analysis/", "enabled",
     "f", "efanrui", "", "web", "", "", "10/17/2025 10:07"],
    # is_recommended -> is_trending.
    ["31", "SST Assistant", "sst", "Agent", "Development",
     "https://radioai.ews.gic.ericsson.se/sst/", "enabled",
     "t", "eosougn", "", "web", "", "", "39:35.3"],
    # Not an agent: excluded by the owner of the data.
    ["40", "TxChangeCycle Explorer", "kb", "Knowledge", "Study",
     "https://radioai.ews.gic.ericsson.se/downloads/tx_change_cycle/", "enabled",
     "f", "eyzxuho", "R1.0", "", "", "", "R1.0"],
    # url has the Gerrit change title pasted in front of the link.
    ["44", "DP slot info decode", "dp", "Agent", "Test",
     "20139529: NOREF: DP slot info decode | "
     "https://gerrit-sigma.gic.ericsson.se/c/radio/ai-assets/+/20139529",
     "enabled", "f", "eyzxuho", "", "repo", "", "", "19:02.5"],
    # url is a whole shell command, and the author is outside BNEW RTE RP.
    ["50", "AFV Agent", "afv", "Agent", "Test|CI",
     'git clone "ssh://zolxuyi@gerrit-sigma.gic.ericsson.se:29418/zolxuyi/'
     'youAgent" && (cd "youAgent" && curl -Lo hooks/commit-msg ...)',
     "enabled", "f", "zolxuyi", "R1.0", "repo", "", "", "14:23.8"],
]


@pytest.fixture
def csv_path(tmp_path):
    path = tmp_path / "export.csv"
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(HEADER)
    writer.writerows(ROWS)
    path.write_text(buf.getvalue(), encoding="utf-8")
    return str(path)


def _run(monkeypatch, csv_path: str, *extra: str) -> int:
    monkeypatch.setattr("sys.argv", ["import_radioai_agents.py", csv_path, *extra])
    return imp.main()


def _items() -> dict[str, RpAiMarketItem]:
    with SessionLocal() as db:
        return {
            i.id: i
            for i in db.scalars(
                select(RpAiMarketItem).where(RpAiMarketItem.source == imp.SOURCE)
            )
        }


def test_a_dry_run_writes_nothing(monkeypatch, csv_path):
    assert _run(monkeypatch, csv_path) == 0
    assert _items() == {}


def test_only_the_agent_rows_are_imported(monkeypatch, csv_path):
    """The knowledge-base row is excluded, so 5 CSV rows become 4 items."""
    assert _run(monkeypatch, csv_path, "--apply") == 0
    items = _items()
    assert len(items) == 4
    assert "txchangecycle-explorer" not in items


def test_every_row_is_a_cloud_agent(monkeypatch, csv_path):
    """Including the two whose access_type said 'repo'.

    Settled deliberately: the recorded addresses are pages to open, not clone
    URLs, so putting them in repo_url would offer a download that lands on a
    review page.
    """
    assert _run(monkeypatch, csv_path, "--apply") == 0
    for item in _items().values():
        assert item.item_type == "agent"
        assert item.service_type == "cloud"
        assert item.repo_url is None
        assert item.service_url


def test_the_author_is_named_and_the_creator_is_a_signum(monkeypatch, csv_path):
    assert _run(monkeypatch, csv_path, "--apply") == 0
    item = _items()["radio-log-analysis"]
    assert item.author == "Ruijun Fan"
    assert item.creator == "EFANRUI"
    assert item.category_id == "radio-sw"


def test_an_author_outside_the_organisation_is_credited_to_their_manager(
    monkeypatch, csv_path
):
    """ZOLXUYI is an external contractor with no department.

    A creator the report cannot place contributes to nobody, so the credit goes
    to their manager — while ``author`` still names who built it, so the card does
    not misattribute the work.
    """
    assert _run(monkeypatch, csv_path, "--apply") == 0
    item = _items()["afv-agent"]
    assert item.creator == "ECUIDEN"
    assert item.author == "You Li (EXT)"
    assert item.category_id == "radio-sw", "placed by the manager's department"


def test_the_two_unusable_urls_are_rewritten(monkeypatch, csv_path):
    """Both were pasted by hand into a field that expects an address."""
    assert _run(monkeypatch, csv_path, "--apply") == 0
    items = _items()
    assert items["dp-slot-info-decode"].service_url == (
        "https://gerrit-sigma.gic.ericsson.se/c/radio/ai-assets/+/20139529"
    )
    afv = items["afv-agent"].service_url
    assert afv.startswith("https://")
    assert "git clone" not in afv


def test_is_recommended_becomes_trending(monkeypatch, csv_path):
    assert _run(monkeypatch, csv_path, "--apply") == 0
    items = _items()
    assert items["sst-assistant"].is_trending is True
    assert items["radio-log-analysis"].is_trending is False


def test_the_source_marks_the_batch(monkeypatch, csv_path):
    """The only way to find these rows again, correct them, or remove them."""
    assert _run(monkeypatch, csv_path, "--apply") == 0
    assert imp.SOURCE == "radioai"
    assert len(_items()) == 4


def test_a_second_run_refuses_instead_of_duplicating(monkeypatch, csv_path):
    """The market does not dedupe on name, so the script has to.

    For a one-off import a repeat run is far more likely than two agents that
    genuinely share a name.
    """
    assert _run(monkeypatch, csv_path, "--apply") == 0
    assert _run(monkeypatch, csv_path, "--apply") == 1
    assert len(_items()) == 4
