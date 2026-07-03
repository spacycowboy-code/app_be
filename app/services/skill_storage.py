"""Skill storage service.

Stores each uploaded skill as a folder inside the git-managed skill repo:

    <repo>/<categorySlug>/<id>_<eid>_<name>/
        manifest.json     # structured metadata
        SKILL.md          # skill definition / instructions
        artifacts/        # uploaded raw file(s)

Each upload bumps the manifest version, commits, tags (``<id>@<version>``)
and (optionally) pushes to the remote with a small retry/sleep guard.

The git author identity is passed per-command so global git config is never
modified.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings

settings = get_settings()

# Characters unsafe for a path segment; keep unicode (Chinese) letters.
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WS = re.compile(r"\s+")


@dataclass
class StoreResult:
    storage_path: str          # relative path within the repo
    version: str
    commit_sha: str | None
    tag: str | None
    artifact_name: str | None
    pushed: bool


def sanitize_segment(text: str, fallback: str = "skill") -> str:
    """Make a string safe to use as a single path segment (keeps unicode)."""
    text = _UNSAFE.sub("", text or "")
    text = text.replace("..", "").strip().strip(".")
    text = _WS.sub("_", text)
    return text[:80] or fallback


def bump_minor(version: str | None) -> str:
    """Bump the minor component: 1.2.0 -> 1.3.0. Default first version 1.0.0."""
    if not version:
        return "1.0.0"
    parts = re.findall(r"\d+", version)
    if not parts:
        return "1.0.0"
    major = int(parts[0])
    minor = int(parts[1]) if len(parts) > 1 else 0
    return f"{major}.{minor + 1}.0"


class SkillStorage:
    def __init__(self) -> None:
        self.repo = Path(settings.skill_repo_path)

    # --- git helpers --------------------------------------------------------
    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        cmd = [
            "git",
            "-C",
            str(self.repo),
            "-c",
            f"user.name={settings.git_author_name}",
            "-c",
            f"user.email={settings.git_author_email}",
            *args,
        ]
        return subprocess.run(
            cmd, capture_output=True, text=True, check=check, timeout=120
        )

    def _current_sha(self) -> str | None:
        try:
            return self._git("rev-parse", "HEAD").stdout.strip() or None
        except subprocess.CalledProcessError:
            return None

    def _push_with_guard(self, tag: str | None) -> bool:
        """Push branch (and tag) with a small retry/sleep guard."""
        if not settings.skill_repo_push:
            return False
        remote = settings.skill_repo_remote
        branch = settings.skill_repo_branch
        last_err = ""
        for attempt in range(3):
            try:
                self._git("push", remote, f"HEAD:{branch}")
                if tag:
                    self._git("push", remote, tag)
                return True
            except subprocess.CalledProcessError as exc:
                last_err = (exc.stderr or "").strip()
                # Someone else pushed first — rebase onto remote and retry.
                try:
                    self._git("pull", "--rebase", remote, branch, check=False)
                except subprocess.CalledProcessError:
                    pass
                time.sleep(2 * (attempt + 1))  # backoff
        print(f"[skill-storage] push failed after retries: {last_err}")
        return False

    # --- public API ---------------------------------------------------------
    def folder_name(self, skill_id: str, eid: str, name: str) -> str:
        return f"{sanitize_segment(skill_id, 'id')}_{sanitize_segment(eid, 'anon')}_{sanitize_segment(name)}"

    def store(
        self,
        *,
        skill_id: str,
        eid: str,
        name: str,
        category_slug: str,
        manifest: dict,
        instructions: str | None,
        artifact_bytes: bytes | None,
        artifact_filename: str | None,
        current_version: str | None,
        existing_storage_path: str | None,
        action: str = "upload",
    ) -> StoreResult:
        """Write/refresh a skill folder, bump version, commit, tag and push."""
        category_slug = sanitize_segment(category_slug, "uncategorized")

        # Stable folder: reuse the existing path if the skill already exists.
        if existing_storage_path:
            rel = Path(existing_storage_path)
        else:
            rel = Path(category_slug) / self.folder_name(skill_id, eid, name)
        skill_dir = self.repo / rel
        (skill_dir / "artifacts").mkdir(parents=True, exist_ok=True)

        version = bump_minor(current_version)

        # Write skill definition.
        (skill_dir / "SKILL.md").write_text(instructions or f"# {name}\n", encoding="utf-8")

        # Save uploaded artifact (if any).
        artifact_name = None
        if artifact_bytes is not None and artifact_filename:
            artifact_name = sanitize_segment(Path(artifact_filename).name, "artifact")
            (skill_dir / "artifacts" / artifact_name).write_bytes(artifact_bytes)

        # Write manifest.
        full_manifest = {
            **manifest,
            "id": skill_id,
            "version": version,
            "uploaderEid": eid,
            "categorySlug": category_slug,
            "artifactName": artifact_name,
            "storedAt": datetime.now(timezone.utc).isoformat(),
        }
        (skill_dir / "manifest.json").write_text(
            json.dumps(full_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Commit.
        self._git("add", "--", str(rel))
        commit_msg = f"{action}({category_slug}): {name} v{version}"
        try:
            self._git("commit", "-m", commit_msg)
        except subprocess.CalledProcessError as exc:
            # "nothing to commit" — treat as no-op but still return current sha.
            if "nothing to commit" not in (exc.stdout or "") + (exc.stderr or ""):
                raise

        tag = f"{sanitize_segment(skill_id, 'id')}@{version}"
        try:
            self._git("tag", "-a", tag, "-m", commit_msg)
        except subprocess.CalledProcessError:
            tag = None  # tag may already exist; don't fail the upload

        sha = self._current_sha()
        pushed = self._push_with_guard(tag)

        return StoreResult(
            storage_path=str(rel),
            version=version,
            commit_sha=sha,
            tag=tag,
            artifact_name=artifact_name,
            pushed=pushed,
        )


skill_storage = SkillStorage()
