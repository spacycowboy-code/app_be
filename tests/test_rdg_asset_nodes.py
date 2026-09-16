"""资产 -> R&D Guide 流程节点的关联。

这个功能的全部意义是 R&D Guide **节点**卡片上那一行「门户里有 N 个相关资产」,所以测试的重心
不是「能不能写进去」,而是**那个数字在各种状态下对不对**:已下架的算不算、改了分类的算不算、
点开的清单和数字对不对得上。数字错了不报错,只是错 —— 这类缺陷只有测试能挡。

**2026-09-16 关联的对象换了一层**(原 tests/test_rdg_asset_tags.py):原先关工具名,现在关流程
节点。Miao 的原话:「本来就是数字资产(tool/skill/agent)和 workflow 的关联」,例子是 PCS、
OA Scope。所以这里的夹具建的是 ``RpAiRdgNode`` 而不只是工具映射。
"""
import pytest

from app.database import SessionLocal
from app.models.category import RpAiCategory
from app.models.market import RpAiMarketItem
from app.models.rdg import RpAiRdgAssetNode, RpAiRdgNode
from app.models.skill import RpAiSkill
from app.services import rdg_tags

RSW = "radio-sw"

#: 两道真实工序 + 一个排版容器。容器必须在夹具里,否则「容器不能被选」那条测的是空气。
PCS = "pcs"
OA_SCOPE = "oa-scope"
CONTAINER = "pg-dev-1"


@pytest.fixture
def guide():
    """R&D Guide 的流程 —— 这就是「可以关联到哪里」的唯一来源。"""
    with SessionLocal() as db:
        db.add(RpAiCategory(id=RSW, label="Radio SW"))
        db.add(RpAiCategory(id="ivc", label="IVC"))
        db.add(RpAiRdgNode(stage_key="early-phase", node_key=PCS, label="PCS",
                           node_type="node", sort_order=1))
        db.add(RpAiRdgNode(stage_key="early-phase", node_key=OA_SCOPE, label="OA Scope",
                           node_type="node", sort_order=2))
        # 排版容器:存在于 rp_ai_rdg_node 里,但**不是一道工序**,不该能被选。
        db.add(RpAiRdgNode(stage_key="development", node_key=CONTAINER, label="",
                           node_type="parallel-group", sort_order=1))
        db.commit()
    yield


def _item(db, item_id, *, category=RSW, status="approved", name=None):
    db.add(RpAiMarketItem(
        id=item_id, name=name or item_id, item_type="tool", service_type="code",
        category_id=category, status=status, source="upload",
    ))


def _skill(db, skill_id, *, category=RSW, status="approved", name=None):
    db.add(RpAiSkill(
        id=skill_id, name=name or skill_id, category_id=category, status=status,
    ))


# --- 候选清单 ---------------------------------------------------------------

def test_the_candidate_list_is_derived_from_the_guide_not_a_second_list(guide):
    """下拉框的内容就是 R&D Guide 挂着的工具,去重、排序。

    这一条钉住「单一来源」:清单来自 tool-mappings 而不是另写的一份名单。写死一份名单也能让
    这条通过,所以下面那条断言去重 —— 一份手写的名单不会有重复,而派生出来的会。
    """
    with SessionLocal() as db:
        assert rdg_tags.valid_node_keys(db) == sorted([OA_SCOPE, PCS])


def test_an_unknown_node_key_is_rejected_with_all_the_bad_keys(guide):
    with SessionLocal() as db:
        msg = rdg_tags.rejection_for(db, [PCS, "Nope", "Also Nope"], RSW)
    assert msg is not None
    # 一次说清,别让人试三遍。
    assert "Nope" in msg and "Also Nope" in msg
    assert PCS not in msg


def test_only_rsw_assets_may_be_linked(guide):
    with SessionLocal() as db:
        assert rdg_tags.rejection_for(db, [PCS], RSW) is None
        # 名字是对的,分类不对 —— 拒的理由必须是分类,不能是「没有这个工具」。
        msg = rdg_tags.rejection_for(db, [PCS], "ivc")
    assert msg is not None
    assert "Radio SW" in msg


def test_choosing_nothing_is_not_an_error(guide):
    """不选是这个字段的正常状态。空清单在任何分类下都放行。"""
    with SessionLocal() as db:
        assert rdg_tags.rejection_for(db, [], "ivc") is None
        assert rdg_tags.rejection_for(db, ["", "  "], None) is None


# --- 写入 -------------------------------------------------------------------

def test_one_asset_can_carry_several_nodes(guide):
    """明确定过的口径:一个资产可以绑多个。"""
    with SessionLocal() as db:
        _item(db, "gl-1")
        db.commit()
        rdg_tags.set_tags(db, "item", "gl-1", [OA_SCOPE, PCS])
        db.commit()
        # tags_for 按 node_key 排序返回,不是按传入顺序 —— 界面按阶段自己排，顺序稳定比
        # 保留输入顺序有用。
        assert rdg_tags.tags_for(db, "item", "gl-1") == sorted([PCS, OA_SCOPE])


def test_writing_replaces_instead_of_appending(guide):
    """整体替换 —— 追加的话界面上取消勾选就没有任何办法真的取消掉。"""
    with SessionLocal() as db:
        _item(db, "gl-1")
        db.commit()
        rdg_tags.set_tags(db, "item", "gl-1", [PCS, OA_SCOPE])
        rdg_tags.set_tags(db, "item", "gl-1", [PCS])
        db.commit()
        assert rdg_tags.tags_for(db, "item", "gl-1") == [PCS]


def test_writing_the_same_pair_twice_does_not_duplicate(guide):
    """约束在库上,不靠写入侧记得先查一遍 —— 有三条写入路径。"""
    with SessionLocal() as db:
        _item(db, "gl-1")
        db.commit()
        rdg_tags.set_tags(db, "item", "gl-1", [PCS, PCS])
        db.commit()
        rows = db.query(RpAiRdgAssetNode).filter_by(asset_id="gl-1").all()
    assert len(rows) == 1


def test_skills_and_items_do_not_collide_on_the_same_id(guide):
    """两种资产的 id 各自独立,同名 id 是可能的。种类必须参与定位。"""
    with SessionLocal() as db:
        _item(db, "same-id")
        _skill(db, "same-id")
        db.commit()
        rdg_tags.set_tags(db, "item", "same-id", [PCS])
        rdg_tags.set_tags(db, "skill", "same-id", [OA_SCOPE])
        db.commit()
        assert rdg_tags.tags_for(db, "item", "same-id") == [PCS]
        assert rdg_tags.tags_for(db, "skill", "same-id") == [OA_SCOPE]


# --- 卡片上那个数字 ---------------------------------------------------------

def test_the_count_covers_both_kinds_of_asset(guide):
    with SessionLocal() as db:
        _item(db, "gl-1")
        _skill(db, "sk-1")
        db.commit()
        rdg_tags.set_tags(db, "item", "gl-1", [PCS])
        rdg_tags.set_tags(db, "skill", "sk-1", [PCS])
        db.commit()
        assert rdg_tags.counts_by_node(db) == {PCS: 2}


def test_an_archived_skill_is_not_counted(guide):
    """技能"删除"是置 archived、行还留着。算进去的话卡片上的数字点进去是空的。

    这一条是这个功能最容易悄悄坏掉的地方:只按关联表 group by 也能让上面那条通过。
    """
    with SessionLocal() as db:
        _skill(db, "sk-live")
        _skill(db, "sk-gone", status="archived")
        db.commit()
        rdg_tags.set_tags(db, "skill", "sk-live", [PCS])
        rdg_tags.set_tags(db, "skill", "sk-gone", [PCS])
        db.commit()
        assert rdg_tags.counts_by_node(db) == {PCS: 1}


def test_a_pending_item_is_not_counted(guide):
    """市场条目同理:没 approved 的在市场上看不见,不该出现在卡片的计数里。"""
    with SessionLocal() as db:
        _item(db, "gl-live")
        _item(db, "gl-pending", status="pending")
        db.commit()
        rdg_tags.set_tags(db, "item", "gl-live", [PCS])
        rdg_tags.set_tags(db, "item", "gl-pending", [PCS])
        db.commit()
        assert rdg_tags.counts_by_node(db) == {PCS: 1}


def test_a_node_with_no_assets_is_absent_rather_than_zero(guide):
    """0 不返回 —— 一张写着「0 个相关资产」的卡片是噪声。"""
    with SessionLocal() as db:
        _item(db, "gl-1")
        db.commit()
        rdg_tags.set_tags(db, "item", "gl-1", [PCS])
        db.commit()
        counts = rdg_tags.counts_by_node(db)
    assert counts == {PCS: 1}
    assert OA_SCOPE not in counts


def test_the_list_behind_the_count_matches_the_count(guide):
    """「卡片上写 3 个」和「点开看到 3 条」用同一道过滤 —— 两处用不同判据是这类计数最常见的坏法。"""
    with SessionLocal() as db:
        _item(db, "gl-1", name="zeta tool")
        _skill(db, "sk-1", name="alpha skill")
        _skill(db, "sk-hidden", name="hidden skill", status="archived")
        db.commit()
        for kind, aid in (("item", "gl-1"), ("skill", "sk-1"), ("skill", "sk-hidden")):
            rdg_tags.set_tags(db, kind, aid, [PCS])
        db.commit()

        count = rdg_tags.counts_by_node(db)[PCS]
        listed = rdg_tags.assets_for_node(db, PCS)
    assert count == len(listed) == 2
    # 合并之后才按名字排,否则 skill 会永远排在 tool/agent 前面 —— 那是表的顺序。
    assert [a["name"] for a in listed] == ["alpha skill", "zeta tool"]
    assert {a["kind"] for a in listed} == {"skill", "item"}


def test_an_unknown_node_key_lists_nothing_rather_than_erroring(guide):
    with SessionLocal() as db:
        assert rdg_tags.assets_for_node(db, "No Such Tool") == []


# --- 删除 -------------------------------------------------------------------

def test_forgetting_an_asset_drops_only_its_own_rows(guide):
    """这张表刻意没有外键,级联删除得显式做。"""
    with SessionLocal() as db:
        _item(db, "gl-1")
        _item(db, "gl-2")
        db.commit()
        rdg_tags.set_tags(db, "item", "gl-1", [PCS])
        rdg_tags.set_tags(db, "item", "gl-2", [PCS])
        db.commit()
        assert rdg_tags.forget(db, "item", "gl-1") == 1
        db.commit()
        assert rdg_tags.tags_for(db, "item", "gl-1") == []
        assert rdg_tags.tags_for(db, "item", "gl-2") == [PCS]


# --- 端点 -------------------------------------------------------------------

def test_the_endpoints_serve_what_the_card_needs(client, guide):
    with SessionLocal() as db:
        _item(db, "gl-1", name="zeta tool")
        db.commit()
        rdg_tags.set_tags(db, "item", "gl-1", [PCS])
        db.commit()

    r = client.get("/api/rsw/rdg/workflow-nodes")
    assert r.status_code == 200
    # 返回的是结构而不是一串 key:选择器要按阶段分组,所以每条都带 stageKey 与 label。
    assert r.json() == [
        {"nodeKey": PCS, "label": "PCS", "stageKey": "early-phase", "nodeType": "node"},
        {"nodeKey": OA_SCOPE, "label": "OA Scope", "stageKey": "early-phase", "nodeType": "node"},
    ]
    # 容器节点不在里面 —— 它不是一道工序。
    assert CONTAINER not in [n["nodeKey"] for n in r.json()]

    r = client.get("/api/rsw/rdg/node-assets")
    assert r.status_code == 200
    assert r.json() == {PCS: 1}

    # 工具名字里有空格,所以路径参数用了 :path。这条钉住那个决定。
    r = client.get("/api/rsw/rdg/node-assets/pcs")
    assert r.status_code == 200
    assert [a["name"] for a in r.json()] == ["zeta tool"]

    r = client.get("/api/rsw/rdg/asset-tags/item/gl-1")
    assert r.status_code == 200
    assert r.json() == [PCS]


def test_registering_an_item_can_carry_nodes(client, guide):
    r = client.post(
        "/api/market",
        json={
            "name": "My RSW Tool", "itemType": "tool", "serviceType": "code",
            "repoUrl": "https://example.invalid/x", "categoryId": RSW,
            "rdgNodes": [PCS],
        },
        headers={"X-User-Eid": "ECUIDEN"},
    )
    assert r.status_code == 201, r.text
    item_id = r.json()["id"]
    assert client.get(f"/api/rsw/rdg/asset-tags/item/{item_id}").json() == [
        PCS
    ]


def test_registering_with_a_bad_node_key_is_refused_and_writes_nothing(client, guide):
    r = client.post(
        "/api/market",
        json={
            "name": "My RSW Tool", "itemType": "tool", "serviceType": "code",
            "repoUrl": "https://example.invalid/x", "categoryId": RSW,
            "rdgNodes": ["Not A Tool"],
        },
        headers={"X-User-Eid": "ECUIDEN"},
    )
    assert r.status_code == 400
    assert "Not A Tool" in r.json()["detail"]
    # 拒了就不该留下半个条目。
    with SessionLocal() as db:
        assert db.query(RpAiMarketItem).count() == 0


def test_editing_without_the_field_leaves_the_tags_alone(client, guide):
    """``None`` 是「这次不改」。按「清空」处理会在用户改个描述时静默删掉关联。"""
    with SessionLocal() as db:
        _item(db, "gl-1", name="zeta")
        db.commit()
        rdg_tags.set_tags(db, "item", "gl-1", [PCS])
        db.commit()

    r = client.put(
        "/api/market/gl-1",
        json={
            "name": "zeta renamed", "itemType": "tool", "serviceType": "code",
            "repoUrl": "https://example.invalid/x", "categoryId": RSW,
        },
        headers={"X-User-Eid": "ADMINMM"},
    )
    assert r.status_code == 200, r.text
    assert client.get("/api/rsw/rdg/asset-tags/item/gl-1").json() == [PCS]


def test_editing_with_an_empty_list_clears_the_tags(client, guide):
    """``[]`` 是「清空」。和上面那条一起才说清 None 与 [] 不是一回事。"""
    with SessionLocal() as db:
        _item(db, "gl-1", name="zeta")
        db.commit()
        rdg_tags.set_tags(db, "item", "gl-1", [PCS])
        db.commit()

    r = client.put(
        "/api/market/gl-1",
        json={
            "name": "zeta", "itemType": "tool", "serviceType": "code",
            "repoUrl": "https://example.invalid/x", "categoryId": RSW,
            "rdgNodes": [],
        },
        headers={"X-User-Eid": "ADMINMM"},
    )
    assert r.status_code == 200, r.text
    assert client.get("/api/rsw/rdg/asset-tags/item/gl-1").json() == []


def test_moving_an_item_out_of_rsw_drops_its_tags(client, guide):
    """分类改出 RSW,关联跟着走 —— 否则卡片会继续算一个不属于 RSW 的资产。"""
    with SessionLocal() as db:
        _item(db, "gl-1", name="zeta")
        db.commit()
        rdg_tags.set_tags(db, "item", "gl-1", [PCS])
        db.commit()

    r = client.put(
        "/api/market/gl-1",
        json={
            "name": "zeta", "itemType": "tool", "serviceType": "code",
            "repoUrl": "https://example.invalid/x", "categoryId": "ivc",
        },
        headers={"X-User-Eid": "ADMINMM"},
    )
    assert r.status_code == 200, r.text
    assert client.get("/api/rsw/rdg/asset-tags/item/gl-1").json() == []
    with SessionLocal() as db:
        assert rdg_tags.counts_by_node(db) == {}


def test_deleting_an_item_drops_its_tags(client, guide):
    with SessionLocal() as db:
        _item(db, "gl-1", name="zeta")
        db.commit()
        rdg_tags.set_tags(db, "item", "gl-1", [PCS])
        db.commit()

    r = client.delete("/api/market/gl-1", headers={"X-User-Eid": "ADMINMM"})
    assert r.status_code == 204, r.text
    with SessionLocal() as db:
        assert db.query(RpAiRdgAssetNode).count() == 0
