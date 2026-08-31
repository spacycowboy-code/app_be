"""市场卡片上显示的作者是人名，不是工号。

包上传那条路径把上传者的工号写进 ``author``（``agent_upload.py`` 里
``item.author = item.author or acting``），而手工登记的表单让人自己填名字。于是市场页上
``Shawn Xiao`` 和 ``EWNGFEI`` 并排出现 —— 同一个字段两种含义，用户看到的是"zip 上传的卡片
和别的卡片不一样"。

skill 那侧早就有 ``RpAiSkill.creator_label`` 解决了同一个问题，市场项的模型上却没有对应的
属性，前端只能渲染 ``author || creator``。这里钉住补上的那条属性，规则与 skill 侧一致：

* ``author`` 与 creator 的工号不同 —— 它是人选的标签（``Anthropic`` / ``AI Team`` /
  ``RTE RU SW``），照原样显示。
* ``author`` 就是工号 —— 显示这个人的名字，解析不出来时退回工号。工号仍然回答了"谁传的"，
  空标签不回答任何问题。
* 完全没有 creator（GitLab 同步进来的条目）—— 没有人可显示，返回 None。
"""

from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models.market import RpAiMarketItem
from app.models.user import RpAiPortal

PREFIX = "creator-label-probe"
UPLOADER = "EUPLOAD"
NO_ROW = "ENOROWX"


def _item(db, suffix: str, **over) -> str:
    iid = f"{PREFIX}-{suffix}"
    fields = dict(
        id=iid,
        name=f"Probe {suffix}",
        item_type="agent",
        service_type="cloud",
        status="approved",
        tags=[],
    )
    fields.update(over)
    db.add(RpAiMarketItem(**fields))
    return iid


@pytest.fixture()
def seeded():
    with SessionLocal() as db:
        if not db.query(RpAiPortal).filter(RpAiPortal.eid == UPLOADER).first():
            db.add(RpAiPortal(eid=UPLOADER, display_name="Frieda Wang", department="CD"))
        db.flush()
        # 包上传：author 就是上传者工号
        _item(db, "upload", creator=UPLOADER, author=UPLOADER, source="upload")
        # 手工登记：author 是人填的名字
        _item(db, "manual", creator=UPLOADER, author="Shawn Xiao", source="manual")
        # 上传者在用户表里没有行（从未登录过）
        _item(db, "unknown", creator=NO_ROW, author=NO_ROW, source="upload")
        # GitLab 同步：根本没有 creator
        _item(db, "synced", creator=None, author=None, source="gitlab-agents")
        db.commit()
    yield
    with SessionLocal() as db:
        db.query(RpAiMarketItem).filter(
            RpAiMarketItem.id.like(f"{PREFIX}%")
        ).delete(synchronize_session=False)
        db.commit()


def _label(suffix: str) -> str | None:
    with SessionLocal() as db:
        return db.get(RpAiMarketItem, f"{PREFIX}-{suffix}").creator_label


def test_an_uploaded_package_shows_the_persons_name(seeded):
    """The bug as reported: this used to render EUPLOAD on the card."""
    assert _label("upload") == "Frieda Wang"


def test_a_typed_author_label_is_shown_verbatim(seeded):
    """A name someone chose is not second-guessed."""
    assert _label("manual") == "Shawn Xiao"


def test_an_unresolvable_eid_falls_back_to_the_eid(seeded):
    """Still a truthful answer to "who uploaded this"; a blank byline is not."""
    assert _label("unknown") == NO_ROW


def test_a_synced_item_has_nobody_to_show(seeded):
    assert _label("synced") is None


def test_the_read_model_carries_the_label(seeded):
    """The card reads the API, so the property has to reach the schema.

    Asserted separately because adding the model property without exposing it is a
    silent no-op — exactly how the original bug survived: ``uploader_name`` was on
    the schema all along, and nothing filled it.
    """
    from app.schemas.market import MarketItemRead

    with SessionLocal() as db:
        item = db.get(RpAiMarketItem, f"{PREFIX}-upload")
        read = MarketItemRead.model_validate(item, from_attributes=True)

    assert read.creator_label == "Frieda Wang"
    assert read.uploader_name == "Frieda Wang"
    # The raw field is still exposed unchanged — the report and the edit form read it.
    assert read.author == UPLOADER
