"""谁被记成这次操作的人。

会话里的身份优先于表单里声明的身份 —— 否则任何人都能把上传记到别人名下。原来这个文件里还有"扫描器
报出没见过的类别时怎么判"的一半,那属于引擎,已随拆分搬到 skill-audit 仓的 tests/test_categories.py。
"""
import types

import pytest

from app.services.eids import acting_eid


def _req(eid=None):
    """A stand-in for the request the site-wide auth gate has already annotated."""
    state = types.SimpleNamespace()
    if eid is not None:
        state.eid = eid
    return types.SimpleNamespace(state=state)


class TestActingEid:
    def test_the_token_wins_over_the_claim(self):
        """The body says one colleague, the session is another. Only the session is evidence."""
        assert acting_eid(_req("EYINCNG"), "EXINGEN") == "EYINCNG"

    def test_the_claim_is_used_when_there_is_no_session(self):
        """auth_enabled=false is a supported mode; there is no verified identity to prefer."""
        assert acting_eid(_req(), "EXINGEN") == "EXINGEN"

    def test_neither_gives_nothing(self):
        assert acting_eid(_req(), None) is None
        assert acting_eid(_req(), "   ") is None

    def test_both_forms_are_normalized(self):
        assert acting_eid(_req(" eyincng "), None) == "EYINCNG"
        assert acting_eid(_req(), " exingen ") == "EXINGEN"

    def test_a_request_without_state_does_not_raise(self):
        """Anything that reaches these handlers has been through the gate, but a helper
        that throws on an unexpected shape would turn a missing attribute into a 500."""
        assert acting_eid(types.SimpleNamespace(), "EXINGEN") == "EXINGEN"


