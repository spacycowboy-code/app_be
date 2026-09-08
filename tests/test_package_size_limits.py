"""tool/agent 包的两个大小上限。

这一批盯住三件"改错了不会当场报错、只会在某个具体的包上失败"的事:

1. **包的上限和 skill 的上限是两个数字。** 原来共用一个,于是给包放宽会连带把 skill 放宽 ——
   而没人要求那件事,也没人会发现。
2. **解压预算必须 >= 上传上限。** 否则一个刚好合规的压缩包会在解压时被自己的上限拒掉,而报错
   说的是"解压后的总大小超过 N MB",跟用户看到的"我这个包才 190MB"对不上。
3. **上传路径读的是包那一项。** 读错了不会报错,只是上限静默变回 50MB。
"""
from __future__ import annotations

import io
import inspect

import pytest
from fastapi import HTTPException

from app.config import get_settings
from app.routers import agent_upload as au


class _FakeUpload:
    """够用的 UploadFile 替身:只需要 filename 和 read()。"""

    def __init__(self, filename: str, data: bytes) -> None:
        self.filename = filename
        self._buf = io.BytesIO(data)

    async def read(self, size: int = -1) -> bytes:
        return self._buf.read() if size < 0 else self._buf.read(size)


def test_the_package_limit_is_200mb() -> None:
    s = get_settings()
    assert s.max_package_upload_bytes == 200 * 1024 * 1024


def test_the_skill_limit_is_not_dragged_along() -> None:
    """给包放宽不该动 skill。两者是两类东西:skill 是一份技能定义,包是一个完整项目。"""
    s = get_settings()
    assert s.max_upload_bytes == 50 * 1024 * 1024
    assert s.max_package_upload_bytes != s.max_upload_bytes


def test_the_extract_budget_is_at_least_the_upload_limit() -> None:
    """解压预算 >= 上传上限。

    反过来的话,一个刚好合规的压缩包会在解压时被自己的上限拒掉,而那句报错("解压后的总大小
    超过 N MB")跟上传者看到的包体积对不上,没人能从它推出该怎么办。
    """
    s = get_settings()
    assert s.max_package_extract_bytes >= s.max_package_upload_bytes


def test_skill_upload_still_reads_the_shared_setting() -> None:
    """skill 那条路仍然读 max_upload_bytes —— 这次改动不该碰它。"""
    from app.routers import upload as skill_upload

    src = inspect.getsource(skill_upload)
    assert "settings.max_upload_bytes" in src
    assert "max_package_upload_bytes" not in src, "skill 不该读包的上限"


def test_package_upload_reads_the_package_settings() -> None:
    """包上传的两处分别读两项,读错了不会报错、只是上限静默变回 50MB。"""
    read_src = inspect.getsource(au._read_package)
    assert "max_package_upload_bytes" in read_src
    assert "settings.max_upload_bytes" not in read_src

    unpack_src = inspect.getsource(au._unpack)
    assert "max_package_extract_bytes" in unpack_src
    assert "settings.max_upload_bytes" not in unpack_src


def test_an_oversized_package_is_refused_with_413(monkeypatch) -> None:
    """超限答 413 并把上限写进消息里。

    用一个很小的上限驱动,避免在测试里真的造 200MB 数据。
    """
    import asyncio

    monkeypatch.setattr(
        get_settings(), "max_package_upload_bytes", 16, raising=False
    )
    au.settings = get_settings()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(au._read_package(_FakeUpload("x.zip", b"0" * 32), "agent"))
    assert exc.value.status_code == 413
    assert "MB 上限" in str(exc.value.detail)


def test_a_package_within_the_limit_is_accepted(monkeypatch) -> None:
    import asyncio

    monkeypatch.setattr(
        get_settings(), "max_package_upload_bytes", 1024, raising=False
    )
    au.settings = get_settings()
    data = asyncio.run(au._read_package(_FakeUpload("x.zip", b"0" * 64), "agent"))
    assert data == b"0" * 64
