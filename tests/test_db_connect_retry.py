"""建连失败重试一次的行为。

这些测试直接调 ``_connect_with_one_retry``，而不是去制造一次真实的连接失败 —— 后者要么依赖
真的把 DNS 打坏，要么依赖某个 DBAPI 的内部时序，两者都不可靠。最后一条测试补上「钩子真的挂
在 engine 上」，因为函数写对了但没注册，正是这个缺陷原本的形状。
"""
from __future__ import annotations

import pytest
from sqlalchemy import event

from app import database
from app.database import _connect_with_one_retry, engine


class _Dialect:
    """只记录被调了几次、按脚本成功或失败的假 dialect。"""

    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    def connect(self, *cargs: object, **cparams: object) -> object:
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture()
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """把退避换成记录，测试不真的等 0.5 秒。"""
    slept: list[float] = []
    monkeypatch.setattr(database.time, "sleep", lambda s: slept.append(s))
    return slept


def test_first_attempt_wins_without_retrying(no_sleep: list[float]) -> None:
    """第一次就成功时不重试、不退避 —— 正常路径必须和没有这个钩子时一样。"""
    dialect = _Dialect(["conn"])

    assert _connect_with_one_retry(dialect, None, (), {}) == "conn"
    assert dialect.calls == 1
    assert no_sleep == []


def test_retries_once_and_succeeds(no_sleep: list[float]) -> None:
    """第一次失败、第二次成功时，调用方拿到连接，看不到那次失败。"""
    dialect = _Dialect([OSError("Temporary failure in name resolution"), "conn"])

    assert _connect_with_one_retry(dialect, None, (), {}) == "conn"
    assert dialect.calls == 2
    assert no_sleep == [database.CONNECT_RETRY_DELAY]


def test_gives_up_after_the_second_failure(no_sleep: list[float]) -> None:
    """只重试一次：第二次仍失败就抛出，且抛的是第二次那个异常。

    钉住「一次」这个数字。退化成无限重试的话，库真的宕了时每个请求都会挂住、把连接池占满，
    那比快速失败更糟。
    """
    second = OSError("second")
    dialect = _Dialect([OSError("first"), second])

    with pytest.raises(OSError) as caught:
        _connect_with_one_retry(dialect, None, (), {})

    assert caught.value is second
    assert dialect.calls == 2


def test_the_hook_is_actually_registered_on_the_engine() -> None:
    """接线测试：函数写对了但没挂上去，等于什么都没做。"""
    assert event.contains(engine, "do_connect", _connect_with_one_retry)


def test_arguments_are_passed_through_unchanged(no_sleep: list[float]) -> None:
    """重试用的是同一组连接参数，不是一组新猜的。"""
    seen: list[tuple[tuple, dict]] = []

    class _Recording:
        calls = 0

        def connect(self, *cargs: object, **cparams: object) -> object:
            _Recording.calls += 1
            seen.append((cargs, dict(cparams)))
            if _Recording.calls == 1:
                raise OSError("boom")
            return "conn"

    assert _connect_with_one_retry(_Recording(), None, ("host",), {"port": 6446}) == "conn"
    assert seen == [(("host",), {"port": 6446}), (("host",), {"port": 6446})]
