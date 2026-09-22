"""API schemas for the skill security audit (evaluate + enriched upload).

`evaluate` returns the audit verdict as data (never an error), so the upload
wizard can show the real result BEFORE the user commits. The verdict itself is
produced by the pure `audit_skill()` function; publish re-runs it as the
authoritative, non-bypassable gate.
"""
from __future__ import annotations

from app.schemas.common import CamelModel


class AuditFinding(CamelModel):
    """One diagnostic: what, where, and what to change.

    ``file`` may address a file inside the uploaded bundle
    (``bundle.zip!/scripts/x.py``); ``evidence`` is the verbatim snippet that
    triggered it, with secret values already redacted upstream.
    """

    source: str            # the analyzer that produced it, as the scanner named it
    category: str          # the scanner's category, never renamed on our side
    severity: str          # critical | high | medium | low | info
    evidence: str | None = None
    detail: str | None = None
    file: str | None = None
    line: int | None = None
    # The scanner's rule id and AITech code. Sent so a reader can look up exactly what
    # fired instead of relying on our wording for it.
    rule_id: str | None = None
    aitech: str | None = None
    remediation: str | None = None


class AuditCheck(CamelModel):
    """One fixed check in the report, with its own findings as evidence.
    # False = 只作上下文,不影响结论(例如打包规范类)。



    The check list is closed and comes from the policy, so a clear check is
    reported too: the uploader sees what was examined, not only what failed.
    ``degraded`` means the layer behind that check could not run, so it is never
    rendered as clean.
    """

    id: str                # secret | dangerous_instruction | ...
    label: str             # user-facing label
    status: str            # clear | hit | degraded
    description: str = ""            # what this check looks for
    severity: str | None = None      # highest severity among findings, when hit
    note: str | None = None          # why the result is unknown, when degraded
    findings: list[AuditFinding] = []


class SkillEvaluateResult(CamelModel):
    """Response of the evaluate (audit-only, no store) endpoint."""

    state: str             # pass | suspect | danger
    reference: str
    # Why this outcome, in one line. Without it the wizard can only say "needs review"
    # and the author has to infer which finding caused it.
    reason: str = ""
    # The same answer as data, so the wizard can address the author in its own words instead of
    # forwarding a sentence written for a reviewer: how much was examined, and why not all of it.
    # An empty checks list does not mean "nothing found" — it can also mean nobody could look.
    coverage: str = ""
    cause: str | None = None
    checks: list[AuditCheck] = []
    findings: list[AuditFinding] = []   # flat list, kept for the audit trail
    # 这次发布会用哪个版本号，以及为什么 —— 在作者点发布**之前**告诉他。
    #
    # 徽标上的版本号原来完全由平台自增，与作者 SKILL.md 里的 `version` 无关，于是两个数字
    # 各走各路（生产实测 2026-09-22：顶层声明了 version 的 5 个技能，没有一个与徽标一致）。
    # 现在优先用声明值，而
    # 声明值不可用时（已发布过、或不比已发布的新）回落自增 —— 那两种情况才有
    # `version_note`，说明文件里哪个值被跳过了、为什么。作者因此能去改文件，而不是发布完
    # 在详情页上发现对不上。
    planned_version: str | None = None
    # 空串表示没什么要解释的：声明值被直接采用，或者作者压根没声明。
    version_note: str = ""


class SkillAuditReport(CamelModel):
    """一个**已存在**技能当前发布版本的审计结论,面向详情页的只读展示。

    与 ``SkillEvaluateResult`` 的区别是刻意的:那个是上传向导在提交前给作者看的实时预览,带完整
    证据(``evidence``);这个是详情页给任何看得到该技能的人看的历史结论,**逐条发现里不含
    ``evidence`` 原文** —— 那是证据片段,可能牵出技能内部实现或扫描规则细节,而详情端点本身不设
    门禁(知道 id 即可访问)。类别、严重级和处置建议足以说明"审计发现了什么、该怎么改"。

    ``available`` 为假表示这个技能没有可展示的审计记录(老数据,或从未审计过),此时其余字段为空,
    前端据此显示"未审计"而不是"通过"。
    """

    available: bool = False
    # pass | suspect | danger（danger 一般不会出现在详情页:那种技能不会被存储）。
    state: str | None = None
    # 为什么是这个结论,一句话。
    reason: str = ""
    coverage: str = ""
    # 需关注的检查项:命中了 critical/high 的发现,或某层未跑完(degraded)。这些是把技能推向
    # 人工复核的原因。
    checks: list[AuditCheck] = []
    # 已放行的低危项:扫描扫出来但**不影响放行**的发现(severity 在 medium 及以下)。单独一组,
    # 让用户看到"扫到了这些、都放行了",而不是把它们和需关注项混在一起或干脆不显示。
    low_risk_checks: list[AuditCheck] = []
