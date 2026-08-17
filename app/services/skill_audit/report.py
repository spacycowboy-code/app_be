"""Roll the audit result up into the report the upload wizard renders.

The checks are named in the reader's language, but the categories underneath are the
scanner's. That split matters: the label is presentation, and a presentation table that
silently drops an unknown category would recreate the renaming problem in the UI. So an
unmapped category is shown with the scanner's own wording instead of being folded into
"other".

Rules kept deliberately simple:
  * a check's ``status`` is a roll-up of its findings, never an average;
  * ``severity`` is the highest severity among that check's findings;
  * a layer that could not run is reported as such, so "we could not tell" is never
    rendered as "clean".
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.services.skill_audit.scan import INCOMPLETE_RULES
from app.services.skill_audit.types import AuditResult, Finding
from app.services.skill_audit.verdict import HYGIENE_RULES

# The scanner's category -> what to call it to an author, and what it looks for. Order is
# the display order. Categories the scanner adds later fall through to their own title.
LABELS: dict[str, tuple[str, str]] = {
    "hardcoded_secrets": ("敏感信息", "写进文件里的真实密钥、令牌或口令。"),
    "prompt_injection": ("提示注入", "让助手忽略既有规则的文字。"),
    "obfuscation": ("混淆与隐藏", "用不可见字符、形似字母或编码把内容藏起来。"),
    "unicode_steganography": ("隐写字符", "藏在文本里的零宽字符与双向控制符。"),
    "command_injection": ("命令与代码注入", "把外部输入拼进命令、SQL 或 eval 的写法。"),
    "data_exfiltration": ("数据外传", "把数据或凭据发往外部的行为。"),
    "context_exfiltration": ("上下文外泄", "把会话内容或系统提示带出去。"),
    "unauthorized_tool_use": ("越权使用工具", "使用了声明之外的工具或网络。"),
    "tool_poisoning": ("工具投毒", "篡改工具定义或让助手改用被替换的工具。"),
    "agent_manipulation": ("操纵助手", "改变助手的判断或让它绕过既有约束。"),
    "excessive_autonomy": ("过度自主", "跳过确认、自行连续执行有副作用的动作。"),
    "autonomy_abuse": ("过度自主", "跳过确认、自行连续执行有副作用的动作。"),
    "privilege_escalation": ("提权", "获取或绕过它本不该拥有的权限。"),
    "persistence": ("持久化驻留", "让自己在任务结束后继续存在。"),
    "supply_chain": ("供应链", "依赖或外部产物带来的风险。"),
    "supply_chain_attack": ("供应链", "依赖或外部产物带来的风险。"),
    "social_engineering": ("社会工程", "诱导用户交出凭据或执行危险操作。"),
    "resource_abuse": ("资源滥用", "死循环、无界重试或耗尽资源的写法。"),
    "skill_discovery_abuse": ("能力虚标", "描述与实际能力不符,或用关键词钓取调用。"),
    "transitive_trust_abuse": ("间接信任滥用", "从外部来源引入指令并照做。"),
    "pii_exposure": ("个人信息", "个人可识别信息出现在内容里。"),
    "data_poisoning": ("数据投毒", "污染训练或检索数据。"),
    "model_abuse": ("模型滥用", "以违规方式使用模型能力。"),
    "malware": ("已知恶意代码", "匹配到已知恶意特征。"),
    "policy_violation": ("规范与可读性", "打包规范问题,以及系统读不了的文件。"),
}

# What to change when the scanner does not say. Its own remediation always wins.
_REMEDIATION = {
    "hardcoded_secrets": "把凭据从文件里去掉并轮换,运行时从配置或环境变量读取。",
    "prompt_injection": "删掉推翻助手既有规则的那段文字。",
    "obfuscation": "删掉不可见字符与形似字母,用普通字符书写。",
    "unicode_steganography": "删掉零宽字符与双向控制符。",
    "policy_violation": "按提示补齐打包信息;读不了的可执行文件请改为平铺的源码。",
}

_UNKNOWN_HINT = "扫描器报出的类别,我们没有对应的中文说明,按它自己的描述阅读。"
_INCOMPLETE_NOTE = "这一层没有跑完,所以结果未知——该技能转人工复核,而不是直接通过。"
_SEV_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}


@dataclass
class Check:
    id: str
    label: str
    status: str                      # clear | hit | incomplete
    description: str = ""
    severity: str | None = None
    note: str | None = None
    gating: bool = True              # False = 只作上下文,不影响结论
    findings: list[Finding] = field(default_factory=list)


def build_checks(audit: AuditResult) -> list[Check]:
    """Group findings by the scanner's category, in the reader's language."""
    by_cat: dict[str, list[Finding]] = {}
    for f in audit.findings:
        by_cat.setdefault(f.category, []).append(f)

    checks: list[Check] = []
    seen: set[str] = set()

    def add(cid: str, found: list[Finding]) -> None:
        label, description = LABELS.get(cid, (cid.replace("_", " "), _UNKNOWN_HINT))
        for f in found:
            if not f.remediation:
                f.remediation = _REMEDIATION.get(f.category)
        # Hygiene findings are facts about packaging: shown, never gating.
        gating = not all(f.rule_id in HYGIENE_RULES for f in found)
        worst = max(found, key=lambda f: _SEV_ORDER.get(f.severity, 0))
        checks.append(Check(cid, label, "hit", description, severity=worst.severity,
                            gating=gating, findings=found))
        seen.add(cid)

    for cid in LABELS:
        if by_cat.get(cid) and cid not in seen:
            add(cid, by_cat[cid])
    for cid, found in by_cat.items():          # categories we have no label for
        if cid not in seen:
            add(cid, found)

    if audit.incomplete:
        checks.append(Check("incomplete", "未能核实的检查", "incomplete",
                            "某一层没有跑完,结论按未核实处理。",
                            note=f"{_INCOMPLETE_NOTE}({', '.join(audit.incomplete)})"))
    return checks


def incomplete_findings(audit: AuditResult) -> list[Finding]:
    """The findings by which the scanner reported its own failure."""
    return [f for f in audit.findings if f.rule_id in INCOMPLETE_RULES]
