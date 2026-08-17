"""Zip layout helpers shared by the skill and market download endpoints.

Both endpoints hand the user a zip that came from somewhere else — an author's
upload, or GitLab's repository archive — and both have to answer the same
question: **what does the user see after extracting?**

Left alone, GitLab names its wrapper folder ``<project>-<ref>-<sha>``, so a
download extracts to something like ``report-manhour-main-3f8a9c2e/``: one level
deeper than expected, under a name nobody can predict or type. Every instruction
that says ``cd report-manhour`` is then wrong.

The rule implemented here is the one ``skills.py`` already follows: strip whatever
wrapper the source used, then add exactly one wrapper we chose. Not "strip and
leave the files bare" — extracting that into the current directory scatters dozens
of files into whatever folder the user happened to be in.
"""
from __future__ import annotations

import io
import zipfile

__all__ = ["common_top_dir", "rewrap_single_top_dir"]


def common_top_dir(members: list[zipfile.ZipInfo]) -> str | None:
    """The single top-level directory shared by every member, else ``None``.

    ``None`` covers both "files sit at the archive root" and "there are several
    top-level entries" — in either case there is no redundant wrapper to strip,
    and guessing one would move files the caller never asked to move.
    """
    tops: set[str] = set()
    for member in members:
        head, sep, _ = member.filename.partition("/")
        if not sep:  # a file at the archive root -> no shared wrapper
            return None
        tops.add(head)
    return next(iter(tops)) if len(tops) == 1 else None


def rewrap_single_top_dir(blob: bytes, top_name: str) -> bytes:
    """Re-pack ``blob`` so everything sits under exactly one ``top_name/``.

    Returns the input unchanged when it is not a readable zip: a download that
    arrives corrupt should fail as "corrupt", not as "empty after repacking".

    Directory entries are dropped and recreated implicitly by the file paths.
    Keeping them would mean rewriting their names too, and a stale directory
    entry pointing at the old wrapper makes some extractors recreate it.
    """
    if not zipfile.is_zipfile(io.BytesIO(blob)):
        return blob

    with zipfile.ZipFile(io.BytesIO(blob)) as src:
        members = [m for m in src.infolist() if not m.is_dir()]
        if not members:
            return blob
        strip = common_top_dir(members)
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
            for member in members:
                name = member.filename
                if strip:
                    name = name[len(strip) + 1 :]
                if not name:
                    continue
                dst.writestr(f"{top_name}/{name}", src.read(member))
    return out.getvalue()
