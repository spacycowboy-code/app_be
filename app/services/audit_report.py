"""把结论渲染成上传向导要看的报告。

引擎给的是事实和分类:每条发现属于哪个类别、多严重,以及哪几层没跑完。**叫什么名字、按什么顺序读、
"没跑完"那句话怎么说,是门户的事** —— 引擎里放中文标签的话,任何别的调用方都会继承门户的话术,而门户
改一个错别字要发引擎版本。整条边界见 `.kiro/specs/skill-audit/service-split.md`。

两处刻意的选择:

* **未知类别照原样显示**,不折叠进"其它"。扫描器升版会带来新类别,而新类别正是最不该消失的东西。
* **"未能核实"那一行的状态是 `degraded`**,不是引擎内部用的 `incomplete`。前端判的就是 `degraded`
  (两处:严重度色块给 indeterminate、标签给 "Not verified");发 `incomplete` 过去会落到 else 分支,
  于是"这一层没跑完、结果未知"被渲染成一条发现加一个严重度色块。措辞归门户,就是指这种事由门户负责对上。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.services.audit_engine import AuditResult, Finding

# 扫描器的类别 -> 对作者怎么称呼它、它在找什么。顺序就是展示顺序。
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

_UNKNOWN_HINT = "扫描器报出的类别,我们没有对应的中文说明,按它自己的描述阅读。"
_INCOMPLETE_NOTE = "这一层没有跑完,所以结果未知——该技能转人工复核,而不是直接通过。"
_SEV_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}


@dataclass
class Check:
    """报告里的一行。

    没有 `gating` 字段:它曾经存在,但门户只是原样转发,前端类型里没有声明、代码里零引用。等真的有人
    渲染"这一行只是提示、不影响结论"的时候再加,那时它有读者。
    """

    id: str
    label: str
    status: str                      # hit | degraded
    description: str = ""
    severity: str | None = None
    note: str | None = None
    findings: list[Finding] = field(default_factory=list)


def build_checks(audit: AuditResult) -> list[Check]:
    """按类别归并,并按展示顺序排好。

    归并放在门户而不是引擎:放在引擎意味着响应里要再带一份 findings 的拷贝,而那两份拷贝**曾经真的
    不一致**(归并顺手补了处置建议,顶层那份已经序列化过了)。归并要用的东西门户本来就有 —— 每条发现
    带 `category`,结论带 `incomplete`。
    """
    by_cat: dict[str, list[Finding]] = {}
    for f in audit.findings:
        by_cat.setdefault(f.category, []).append(f)

    checks: list[Check] = []
    seen: set[str] = set()

    def add(cid: str, found: list[Finding]) -> None:
        label, description = LABELS.get(cid, (cid.replace("_", " "), _UNKNOWN_HINT))
        worst = max(found, key=lambda f: _SEV_ORDER.get(f.severity, 0))
        checks.append(Check(cid, label, "hit", description,
                            severity=worst.severity, findings=found))
        seen.add(cid)

    for cid in LABELS:
        if by_cat.get(cid) and cid not in seen:
            add(cid, by_cat[cid])
    for cid, found in by_cat.items():          # 我们没有中文说明的类别
        if cid not in seen:
            add(cid, found)

    if audit.incomplete:
        checks.append(Check("incomplete", "未能核实的检查", "degraded",
                            "某一层没有跑完,结论按未核实处理。",
                            note=f"{_INCOMPLETE_NOTE}({', '.join(audit.incomplete)})"))
    return checks
