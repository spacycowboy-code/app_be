"""GET /api/skills/{id}/audit —— 详情页的只读审计结论。

这个端点面向"任何看得到该技能的人",所以和上传向导的 evaluate 有一条硬性区别:**逐条发现不带
evidence 原文**。evidence 是触发规则的证据片段,可能牵出技能内部实现或扫描规则细节,而详情端点本身
不设门禁(知道 id 即可访问)。类别、严重级、处置建议足以说明"审计发现了什么、该怎么改"。

覆盖三种输入:
* 有审计记录 + findings —— 返回 available=True、state/reason 透传、findings 按类别归并成 checks;
* 有 finding 且带 evidence —— 响应里 evidence 必须为空(这是这个端点存在的理由之一);
* 没有审计记录(老数据 / 从未审计) —— available=False,前端据此显示"未审计"而不是"通过"。
"""
from app.database import SessionLocal
from app.models.user import RpAiPortal
from app.models.skill import RpAiSkill
from app.models.audit import RpAiSkillAuditRecord, RpAiSkillFinding

OWNER = "EOWNER"


def _seed_skill(sid: str, *, with_record: bool, **rec_kw) -> None:
    """建一个技能行,可选地挂一条描述当前发布版的审计记录 + findings。

    分三次提交:技能行的 ``audit_ref`` 与审计记录的 ``skill_id`` 互相引用,套件开了 sqlite 外键
    检查,一次 flush 里两行一起插会撞 FK(照搬 test_archived_stays_archived 的做法)。
    """
    with SessionLocal() as db:
        if db.get(RpAiPortal, OWNER) is None:
            db.add(RpAiPortal(eid=OWNER, display_name="Owner"))
            db.flush()
        db.add(
            RpAiSkill(
                id=sid, name=sid, category_id="public", status="approved",
                version="1.0.0", tags=[], uploader_eid=OWNER,
                storage_path=f"public/{sid}", repo_commit="a" * 40,
                instructions="# x\n", audit_state=rec_kw.get("state", "pass"),
            )
        )
        db.commit()

    if not with_record:
        return

    findings = rec_kw.pop("findings", [])
    with SessionLocal() as db:
        rec = RpAiSkillAuditRecord(
            id="rec-" + sid, skill_id=sid, skill_name=sid, uploader_eid=OWNER,
            replaces_published=False, **rec_kw,
        )
        for f in findings:
            rec.findings.append(RpAiSkillFinding(**f))
        db.add(rec)
        db.commit()

    with SessionLocal() as db:
        db.get(RpAiSkill, sid).audit_ref = "rec-" + sid
        db.commit()


def test_no_record_reports_unavailable(client):
    """老数据 / 从未审计:available=False,其余为空。前端据此显示"未审计"。"""
    _seed_skill("no-audit", with_record=False)
    body = client.get("/api/skills/no-audit/audit").json()
    assert body["available"] is False
    assert body["state"] is None
    assert body["checks"] == []


def test_missing_skill_is_404(client):
    assert client.get("/api/skills/ghost/audit").status_code == 404


def test_pass_record_is_available_and_carries_state_and_reason(client):
    _seed_skill("clean", with_record=True, state="pass", reason="扫描干净",
                coverage="complete")
    body = client.get("/api/skills/clean/audit").json()
    assert body["available"] is True
    assert body["state"] == "pass"
    assert body["reason"] == "扫描干净"
    assert body["coverage"] == "complete"


def test_findings_are_grouped_into_checks(client):
    _seed_skill(
        "flagged", with_record=True, state="suspect",
        reason="high dangerous_instruction", coverage="complete",
        findings=[
            dict(source="judge", category="dangerous_instruction", severity="high",
                 detail="让助手忽略规则", remediation="删掉那段指令", evidence="ignore all rules"),
        ],
    )
    body = client.get("/api/skills/flagged/audit").json()
    assert body["available"] is True
    assert body["state"] == "suspect"
    # 归并成 checks:命中的类别出现,严重级带出来,处置建议保留。
    hit = [c for c in body["checks"] if c["status"] == "hit"]
    assert len(hit) == 1
    assert hit[0]["id"] == "dangerous_instruction"
    assert hit[0]["severity"] == "high"
    assert hit[0]["findings"][0]["remediation"] == "删掉那段指令"


def test_evidence_is_never_exposed(client):
    """这是这个端点区别于 evaluate 的核心约束:证据原文不出现在响应里。"""
    secret_span = "SUPER-SECRET-EVIDENCE-SPAN"
    _seed_skill(
        "leaky", with_record=True, state="suspect", reason="r", coverage="complete",
        findings=[
            dict(source="detect-secrets", category="hardcoded_secrets", severity="critical",
                 detail="hardcoded token", evidence=secret_span),
        ],
    )
    resp = client.get("/api/skills/leaky/audit")
    # 整份响应文本里都不该出现证据原文。
    assert secret_span not in resp.text
    finding = resp.json()["checks"][0]["findings"][0]
    assert finding["evidence"] is None
