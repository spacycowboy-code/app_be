"""Shared builders for a skill's on-disk representation (manifest + SKILL.md).

Used by both the bulk export and the edit/upload flows so the repository
files stay consistent regardless of how a skill was written.
"""
from __future__ import annotations

import re

from app.models.skill import RpAiSkill

_FM_NAME = re.compile(r"(?im)^\s*name\s*:")
_FM_DESC = re.compile(r"(?im)^\s*description\s*:")


def has_name_and_description(text: str) -> bool:
    """True if the markdown contains both ``name:`` and ``description:``."""
    return bool(text) and bool(_FM_NAME.search(text)) and bool(_FM_DESC.search(text))


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
    """Full metadata written to manifest.json (camelCase)."""
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
        "rating": skill.rating,
        "downloads": skill.downloads,
        "isTrending": skill.is_trending,
        "isFree": skill.is_free,
        "ratingDist": skill.rating_dist,
        "reviewCount": skill.review_count,
        "capabilityFull": skill.capability_full,
        "examplePrompt": skill.example_prompt,
        "exampleOutput": skill.example_output,
        "status": skill.status,
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
