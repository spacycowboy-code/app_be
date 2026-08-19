"""Shared builders for a skill's on-disk representation (manifest + SKILL.md).

Used by both the bulk export and the edit/upload flows so the repository
files stay consistent regardless of how a skill was written.
"""
from __future__ import annotations

import io
import re
import zipfile

import yaml

from app.models.skill import RpAiSkill

_FM_NAME = re.compile(r"(?im)^\s*name\s*:")
_FM_DESC = re.compile(r"(?im)^\s*description\s*:")
_FM_NAME_VAL = re.compile(r"(?im)^\s*name\s*:\s*(.+?)\s*$")
_FM_DESC_VAL = re.compile(r"(?im)^\s*description\s*:\s*(.+?)\s*$")
# Leading YAML frontmatter block delimited by --- ... --- (CRLF/LF tolerant).
_FM_BLOCK = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n", re.DOTALL)


def has_name_and_description(text: str) -> bool:
    """True if the markdown contains both ``name:`` and ``description:``."""
    return bool(text) and bool(_FM_NAME.search(text)) and bool(_FM_DESC.search(text))


def extract_skill_md_from_zip(data: bytes) -> str | None:
    """Return the text of ``SKILL.md`` from a zip archive, or ``None``.

    Handles archives where the skill folder is nested one level deep (e.g.
    ``test-skill/SKILL.md``) by picking the shallowest match. The filename
    match is case-insensitive. Returns ``None`` if the bytes are not a valid
    zip, no ``SKILL.md`` is present, or it cannot be decoded as UTF-8.
    """
    if not data or not zipfile.is_zipfile(io.BytesIO(data)):
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            candidates = [
                info
                for info in zf.infolist()
                if not info.is_dir()
                and info.filename.rsplit("/", 1)[-1].lower() == "skill.md"
            ]
            if not candidates:
                return None
            # Prefer the shallowest SKILL.md (fewest path segments).
            candidates.sort(key=lambda i: i.filename.count("/"))
            raw = zf.read(candidates[0])
        return raw.decode("utf-8")
    except (zipfile.BadZipFile, UnicodeDecodeError, KeyError):
        return None


def frontmatter_problem(text: str) -> str | None:
    """Why this skill's frontmatter will not be readable, or None if it is fine.

    ``parse_frontmatter`` is deliberately forgiving: it falls back to a regex so a slightly
    malformed block still yields a name for the marketplace card. Everything downstream is
    not that forgiving. The scanner parses the block as YAML and refuses to load the skill
    when it is not a mapping, which makes the audit end as "no report" — the gate then reads
    that as "not checked" and sends it to a human, who is handed a skill nobody has looked at.
    Measured: one skill uploaded twice, unchanged, because the message it got back never said
    what was wrong.

    The commonest mistake by a wide margin is a missing space after the colon. ``name:foo`` is
    a plain scalar in YAML, not a mapping, so the field reads as absent while looking present.
    """
    if not text:
        return None
    block = _FM_BLOCK.match(text.lstrip("\ufeff"))
    if not block:
        # No delimited block. ``with_frontmatter`` would add one — but only when the labels are
        # absent, and ``has_name_and_description`` matches them anywhere in the file, so a
        # SKILL.md carrying bare `name:` / `description:` lines and no `---` block reaches the
        # scanner unchanged. Measured against the installed scanner: it refuses to load that
        # with "missing required field: name". Same incident, different door.
        if _FM_NAME.search(text) or _FM_DESC.search(text):
            return ("SKILL.md 缺少 frontmatter 的 `---` 分隔块。`name:` 与 `description:` 必须写在"
                    "文件开头的一对 `---` 之间,否则扫描器无法加载这个 skill。正确形状:\n"
                    "```\n---\nname: 你的名字\ndescription: 一句话说明\n---\n```")
        return None
    body = block.group(1)
    try:
        data = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        first = str(exc).splitlines()[0] if str(exc) else "无法解析"
        return f"SKILL.md 的 frontmatter 不是有效的 YAML:{first}"
    if not isinstance(data, dict):
        offenders = [ln.strip() for ln in body.splitlines()
                     if re.match(r"^\s*(name|description)\s*:\S", ln)]
        hint = ""
        if offenders:
            field = offenders[0].split(":", 1)[0].strip()
            hint = (f"——`{offenders[0][:60]}` 里冒号后面少了空格,"
                    f"YAML 会把整段读成一行字符串,于是 `{field}` 被当成没写。"
                    f"改成 `{field}: ` 加空格即可")
        return ("SKILL.md 的 frontmatter 没能解析成键值对" + hint
                + "。正确写法:`name: 你的名字` 与 `description: 一句话说明`,冒号后要有空格。")
    missing = [k for k in ("name", "description") if not str(data.get(k) or "").strip()]
    if missing:
        return (f"SKILL.md 的 frontmatter 缺少 {'、'.join(missing)}。"
                "两个字段都必填,格式为 `name: 你的名字`、`description: 一句话说明`。")
    return None


def parse_frontmatter(text: str) -> tuple[str | None, str | None]:
    """Extract the name and description values from the markdown frontmatter.

    Parses the leading ``---`` YAML block with a real YAML parser so every
    scalar style is handled correctly — plain, quoted, and multi-line folded
    (``>`` / ``>-``) or literal (``|``) block scalars. Falls back to a simple
    per-line regex when there is no ``---`` block or YAML parsing fails.
    """
    if not text:
        return (None, None)

    block_match = _FM_BLOCK.match(text.lstrip("\ufeff"))
    if block_match:
        try:
            data = yaml.safe_load(block_match.group(1))
        except yaml.YAMLError:
            data = None
        if isinstance(data, dict):
            name = data.get("name")
            desc = data.get("description")
            return (
                str(name).strip() if name is not None else None,
                str(desc).strip() if desc is not None else None,
            )

    # Fallback: no/invalid ``---`` block — grab the inline values by regex and
    # strip surrounding quotes (block scalars can't be recovered this way).
    n = _FM_NAME_VAL.search(text)
    d = _FM_DESC_VAL.search(text)

    def _clean(v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().strip('"').strip("'").strip()
        # A lone block-scalar indicator carries no inline text.
        return None if v in (">", ">-", "|", "|-", ">+", "|+") else (v or None)

    return (
        _clean(n.group(1)) if n else None,
        _clean(d.group(1)) if d else None,
    )


def with_frontmatter(body: str, name: str, description: str | None) -> str:
    """Prepend a YAML frontmatter block unless name:/description: already exist."""
    if has_name_and_description(body):
        return body
    desc = (description or "").replace("\n", " ").strip()
    fm = f"---\nname: {name}\ndescription: {desc}\n---\n\n"
    return fm + (body or "")


def render_skill_md(skill: RpAiSkill) -> str:
    """Return the skill definition markdown, guaranteed to carry frontmatter."""
    if skill.instructions and skill.instructions.strip():
        body = skill.instructions
    else:
        body = _generate_body(skill)
    return with_frontmatter(body, skill.name, skill.description)


def _generate_body(skill: RpAiSkill) -> str:
    lines = [f"# {skill.name}", ""]
    if skill.description:
        lines += [skill.description, ""]
    if skill.capability_full:
        lines += ["## 能力说明", skill.capability_full, ""]
    if skill.input_params:
        lines += ["## 输入参数"]
        for p in skill.input_params:
            req = "必填" if p.required else "可选"
            lines.append(f"- `{p.name}` ({p.type or 'string'}, {req}): {p.description or ''}")
        lines.append("")
    if skill.sub_skills:
        lines += ["## 子能力"]
        for s in skill.sub_skills:
            lines.append(f"- **{s.name}**：{s.description or ''}")
        lines.append("")
    if skill.example_prompt or skill.example_output:
        lines += ["## 示例"]
        if skill.example_prompt:
            lines += ["**Prompt**", "", f"> {skill.example_prompt}", ""]
        if skill.example_output:
            lines += ["**Output**", "", "```", skill.example_output, "```", ""]
    return "\n".join(lines)


def build_manifest(skill: RpAiSkill, eid: str, slug: str) -> dict:
    """Authored metadata written to manifest.json (camelCase).

    Only fields a person authored belong here, because the repository is where
    they are edited and reviewed. Measured fields (rating, ratingDist,
    reviewCount, downloads, isTrending) are aggregates of portal activity owned by
    the database: a snapshot in git would be stale from the moment it is written,
    keeping it accurate would mean a commit per download, and the churn would bury
    the instruction diffs a security reviewer has to read. The lifecycle field
    (status) is likewise derived - see skill_audit.reconcile - so it is not
    written either. The importer still reads any of these from older manifests.
    """
    return {
        "id": skill.id,
        "name": skill.name,
        "description": skill.description,
        "category": skill.category,
        "categoryId": skill.category_id,
        "categorySlug": slug,
        "tags": skill.tags or [],
        "version": skill.version or "1.0.0",
        "author": skill.author,
        "uploaderEid": eid,
        "isFree": skill.is_free,
        "capabilityFull": skill.capability_full,
        "examplePrompt": skill.example_prompt,
        "exampleOutput": skill.example_output,
        "inputParams": [
            {"name": p.name, "type": p.type, "required": p.required, "desc": p.description}
            for p in skill.input_params
        ],
        "subSkills": [
            {
                "name": s.name,
                "desc": s.description,
                "badge": s.badge,
                "iconColor": s.icon_color,
                "iconBg": s.icon_bg,
            }
            for s in skill.sub_skills
        ],
        "cases": [{"title": c.title, "image": c.image} for c in skill.cases],
    }
