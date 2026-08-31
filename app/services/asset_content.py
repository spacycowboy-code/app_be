"""技能正文该去哪个上游读。

在这个模块出现之前只有一个上游,``storage_path`` 就是"技能在那个仓里的路径"。现在有两个:
门户自己的技能仓(GitLab),和 Gerrit 上 ``radio/ai-assets`` 那个别人在维护的资产库。

分派依据是 ``storage_path`` 的前缀而不是新加一列:那一列本来就是"内容在哪"的唯一记录,把
上游写进它是同一件事的自然延伸,而多加一列意味着两列可以互相矛盾。

``ContentUnavailable`` 两边同名不同类,所以这里统一成本模块的异常,调用方只 catch 一个。
"""

from __future__ import annotations

from app.repositories import gerrit_content, skill_content

#: ``storage_path`` 上的前缀,标明这份内容住在 Gerrit 上。后面跟的是仓内路径,
#: 例如 ``gerrit:skill/rtm-3211``。
GERRIT_PREFIX = "gerrit:"


class ContentUnavailable(RuntimeError):
    """两个上游任一读不到。调用方不需要知道是哪个上游。"""


def is_gerrit(storage_path: str | None) -> bool:
    return bool(storage_path) and str(storage_path).startswith(GERRIT_PREFIX)


def _strip(storage_path: str) -> str:
    return storage_path[len(GERRIT_PREFIX):].strip("/")


def read_text(storage_path: str, ref: str, relative: str) -> str:
    """读资产目录下的某个文件。``relative`` 相对 ``storage_path``。"""
    try:
        if is_gerrit(storage_path):
            return gerrit_content.read_text(ref, f"{_strip(storage_path)}/{relative}")
        return skill_content.read_text(ref, f"{storage_path}/{relative}")
    except (gerrit_content.ContentUnavailable, skill_content.ContentUnavailable) as exc:
        raise ContentUnavailable(str(exc)) from exc


def read_tree(storage_path: str, ref: str) -> dict[str, bytes]:
    """整棵读下来,``{相对资产目录的路径: 字节}``。"""
    try:
        if is_gerrit(storage_path):
            return gerrit_content.read_tree(ref, _strip(storage_path))
        return skill_content.read_tree(ref, storage_path)
    except (gerrit_content.ContentUnavailable, skill_content.ContentUnavailable) as exc:
        raise ContentUnavailable(str(exc)) from exc
