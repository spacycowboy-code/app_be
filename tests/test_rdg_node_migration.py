"""容器节点不能被关联，以及旧「资产 -> 工具名」关联怎么搬成「资产 -> 流程节点」。

这两件事是 2026-09-16 换关联对象时新增的判断，原来那份测试里没有对应的东西：

- ``parallel-group`` **存在于** ``rp_ai_rdg_node`` 里，但不是一道工序（它自己的 label 是空的）。
  只按「表里有没有这一行」做白名单会把它放进来，而资产挂到容器上，卡片不知道该显示在哪。
- 搬迁靠反查 ``rp_ai_rdg_tool_mapping``。它里面除了真实 node_key 还有 ``stage:development``
  这类阶段级条目，搬进来会得到点不开的卡片。
"""
import pytest
from sqlalchemy import select, text

from app.database import SessionLocal
from app.migrate import _migrate_asset_tags_to_nodes
from app.models.category import RpAiCategory
from app.models.market import RpAiMarketItem
from app.models.rdg import RpAiRdgAssetNode, RpAiRdgNode, RpAiRdgToolMapping
from app.services import rdg_tags

RSW = "radio-sw"


@pytest.fixture
def guide():
    """两道真实工序、一个排版容器，以及一个跨两道工序的工具。"""
    with SessionLocal() as db:
        db.add(RpAiCategory(id=RSW, label="Radio SW"))
        db.add(RpAiRdgNode(stage_key="early-phase", node_key="pcs", label="PCS",
                           node_type="node", sort_order=1))
        db.add(RpAiRdgNode(stage_key="early-phase", node_key="oa-scope", label="OA Scope",
                           node_type="node", sort_order=2))
        db.add(RpAiRdgNode(stage_key="development", node_key="pg-dev-1", label="",
                           node_type="parallel-group", sort_order=1))
        # 第二个容器，**带 label**。这一条是刻意的：真实数据里容器的 label 是空的，所以只有
        # 空 label 的容器时，把它挡在外面的其实是「label 非空」那条过滤，`node_type` 那条完全
        # 没参与 —— 反面探针会不咬，也就是说那条断言在测空气。带 label 的容器让 node_type
        # 成为唯一起作用的判据。
        db.add(RpAiRdgNode(stage_key="development", node_key="pg-labelled", label="Parallel Work",
                           node_type="parallel-group", sort_order=2))
        # DFE Copilot 挂在两道真实工序上，外加一条阶段级条目（后者不是节点）。
        db.add(RpAiRdgToolMapping(node_key="pcs", tool_name="DFE Copilot"))
        db.add(RpAiRdgToolMapping(node_key="oa-scope", tool_name="DFE Copilot"))
        db.add(RpAiRdgToolMapping(node_key="stage:development", tool_name="DFE Copilot"))
        # 只挂在阶段级条目上的工具 —— 搬迁时无处可去。
        db.add(RpAiRdgToolMapping(node_key="stage:test", tool_name="Orphan Tool"))
        db.add(RpAiMarketItem(id="gl-1", name="gl-1", item_type="tool", service_type="code",
                              category_id=RSW, status="approved", source="upload"))
        db.commit()
    yield


def _old_tag(db, kind: str, asset_id: str, tool_name: str) -> None:
    """直接写旧表。用裸 SQL 是刻意的:那张表已经没有代码在读，模型只为搬迁保留。"""
    db.execute(
        text(
            "INSERT INTO rp_ai_rdg_asset_tag (asset_kind, asset_id, tool_name) "
            "VALUES (:k, :a, :t)"
        ),
        {"k": kind, "a": asset_id, "t": tool_name},
    )
    db.commit()


# --- 容器不能被选 -----------------------------------------------------------

def test_a_layout_container_is_not_selectable(guide):
    with SessionLocal() as db:
        keys = rdg_tags.valid_node_keys(db)
    assert "pcs" in keys and "oa-scope" in keys
    # 它在 rp_ai_rdg_node 里确实有一行 —— 所以这一条钉的是「按 node_type 过滤」而不是「表里没有」。
    assert "pg-dev-1" not in keys


def test_a_container_is_excluded_by_its_type_not_by_an_empty_label(guide):
    """把 node_type 那条过滤单独钉住。

    真实数据里容器的 label 是空的，于是「label 非空」那条过滤会顺手把它挡掉，`node_type`
    那条形同虚设 —— 2026-09-16 的反面探针（把 `_NON_SELECTABLE_NODE_TYPES` 清空）**没有咬**，
    就是因为只有空 label 的容器。这一条用一个带 label 的容器让 node_type 成为唯一判据。
    """
    with SessionLocal() as db:
        keys = rdg_tags.valid_node_keys(db)
        labelled = db.scalar(select(RpAiRdgNode).where(RpAiRdgNode.node_key == "pg-labelled"))
    # 对照：这一行必须真的存在且 label 非空，否则本条又变成测空气。
    assert labelled is not None and labelled.label.strip()
    assert "pg-labelled" not in keys


def test_the_container_row_really_exists_so_the_test_above_means_something(guide):
    """对照：容器行必须真的在表里，否则上一条测的是空气。"""
    with SessionLocal() as db:
        row = db.scalar(select(RpAiRdgNode).where(RpAiRdgNode.node_key == "pg-dev-1"))
    assert row is not None
    assert row.node_type == "parallel-group"


def test_linking_to_a_container_is_refused_with_a_readable_reason(guide):
    with SessionLocal() as db:
        msg = rdg_tags.rejection_for(db, ["pg-dev-1"], RSW)
    assert msg is not None
    assert "pg-dev-1" in msg


def test_the_picker_payload_carries_the_stage_so_it_can_be_grouped(guide):
    """78 个节点平铺不能用，所以每条必须带 stageKey —— 少了它前端只能自己再查一次。"""
    with SessionLocal() as db:
        nodes = rdg_tags.selectable_nodes(db)
    assert {n["nodeKey"] for n in nodes} == {"pcs", "oa-scope"}
    assert all(n["stageKey"] for n in nodes)
    assert all(n["label"].strip() for n in nodes)


# --- 搬迁 -------------------------------------------------------------------

def test_an_old_tool_tag_becomes_the_nodes_that_tool_serves(guide):
    """「资产对应工具 T」搬成「资产用在 T 所服务的那几道工序上」。

    一行搬成多行是预期的 —— 一个工具跨多道工序是常态。
    """
    with SessionLocal() as db:
        _old_tag(db, "item", "gl-1", "DFE Copilot")
        _migrate_asset_tags_to_nodes(db)
    with SessionLocal() as db:
        assert rdg_tags.tags_for(db, "item", "gl-1") == ["oa-scope", "pcs"]


def test_stage_level_mappings_are_not_migrated_as_nodes(guide):
    """``stage:development`` 不是流程节点，搬进来会得到点不开的卡片。"""
    with SessionLocal() as db:
        _old_tag(db, "item", "gl-1", "DFE Copilot")
        _migrate_asset_tags_to_nodes(db)
    with SessionLocal() as db:
        keys = rdg_tags.tags_for(db, "item", "gl-1")
    assert not any(k.startswith("stage:") for k in keys)


def test_a_tool_with_no_real_node_is_left_behind_rather_than_guessed(guide):
    """猜一个工序比没有关联更坏 —— 它会让卡片上出现一个没人认领的数字。"""
    with SessionLocal() as db:
        _old_tag(db, "item", "gl-1", "Orphan Tool")
        _migrate_asset_tags_to_nodes(db)
    with SessionLocal() as db:
        assert rdg_tags.tags_for(db, "item", "gl-1") == []


def test_running_the_migration_twice_does_not_duplicate(guide):
    """每次启动都会跑，所以幂等不是可选项。"""
    with SessionLocal() as db:
        _old_tag(db, "item", "gl-1", "DFE Copilot")
        _migrate_asset_tags_to_nodes(db)
        _migrate_asset_tags_to_nodes(db)
    with SessionLocal() as db:
        rows = db.scalars(
            select(RpAiRdgAssetNode).where(RpAiRdgAssetNode.asset_id == "gl-1")
        ).all()
    assert len(rows) == 2


def test_the_old_rows_are_kept_so_a_bad_migration_can_be_redone(guide):
    """搬迁不删旧行。删了就没有第二次机会，而这里只有个位数的行。"""
    with SessionLocal() as db:
        _old_tag(db, "item", "gl-1", "DFE Copilot")
        _migrate_asset_tags_to_nodes(db)
        left = db.execute(text("SELECT COUNT(*) FROM rp_ai_rdg_asset_tag")).scalar()
    assert left == 1


def test_an_empty_old_table_is_a_no_op(guide):
    with SessionLocal() as db:
        _migrate_asset_tags_to_nodes(db)
        assert db.scalars(select(RpAiRdgAssetNode)).all() == []
