"""把 Gerrit ``radio/ai-assets`` 上的资产迁进门户自己的 GitLab,一次性工具。

背景:门户此前是那个 Gerrit 仓的**只读下游** —— 12 个市场条目(id `gerrit-agent-*` /
`gerrit-tool-*`)与 8 个技能(``storage_path`` 形如 ``gerrit:skill/xxx``)的内容都不在门户
库里,而是每次请求实时从 Gerrit 读。后果有两条:那 20 个资产在市场上**下载不了**(下载端点
只服务门户自己保管的包,判据是 id 以 ``gl-`` 开头);而 Gerrit 那边一旦停用或权限收回,这
8 个技能的详情页与下载当场 503,门户手上没有副本。

这个工具把它们搬成门户自己的资产:走**门户自己的上传端点**,不绕过任何一步。

**为什么走 HTTP 端点而不是直调服务层。** 技能上传路径上有一道不可绕的安全审计闸门
(``upload.py`` 的注释原话:"publish re-runs the same audit as the authoritative,
non-bypassable gate")。直调 ``skill_storage`` 会把那道闸绕掉 —— 而这批资产从来没被审计过,
借这次迁移过一次闸是净收益,不是成本。skill 的内容是注入进别人会话的指令,这道闸正是为它设的。

**归属从门户现有的行里读,不重新解析 owner。** Gerrit 那些 manifest 的 ``owner`` 有六种写法
(工号、``姓名 (工号)``、邮箱、纯姓名、两个人、团队名),重新解析一遍会得到与今天不同的答案,
而今天库里那份是人看过、定过的。所以按资产读 ``creator`` / ``uploader_eid`` 原样带过去:14 条
真作者保留,6 条解析不出的仍然记在兜底那个人名下 —— 这是明确定过的口径,不是这里的默认值。

**签票是为了让 creator 跟着资产走。** 两个上传端点的身份都取自 ``acting_eid(request, ...)``,
即签名的会话票优先。所以工具按每个资产的归属人各签一张票,而不是用一个执行者的身份把 20 个
资产全记到自己名下。

**推不出确定启动命令的 agent/tool 不迁。** ``launch`` 在上传端点里是必填,而 manifest 的
schema 里没有这个字段。门户的 ``/inspect`` 能从包里推:结构化来源(``.kiro/`` 的 agent 定义、
``pyproject``、``package.json``)推出来的带 ``certain``,从 README 里抓的不带。**只采用
certain 的** —— 凭 README 猜一条命令写进去,等于让下载的人照一条错命令去跑,比不迁更糟。
不确定的列在报告里让人补一条 launch 再单独迁。

用法(在后端容器里跑,它要连库、Gerrit 与 GitLab):

    python -m tools.migrate_gerrit_assets                      # 空跑,默认
    python -m tools.migrate_gerrit_assets --apply
    python -m tools.migrate_gerrit_assets --only agent/ci-swat --apply
    python -m tools.migrate_gerrit_assets --apply --map /tmp/gerrit-migration-map.json

``--map`` 写出 ``旧 id -> 新 id`` 的对应,搬下载量那一步(见 ``carry_gerrit_downloads.py``)
按它改指向。**没有这份对应就没法搬下载量**,所以 ``--apply`` 时它是必写的。
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.database import SessionLocal
from app.models.market import RpAiMarketItem
from app.models.skill import RpAiSkill
from app.repositories import gerrit_content as gc
from app.services.auth_tokens import create_token

#: 门户自己在容器里的地址。走 HTTP 而不是 ASGI 内调:中间件、鉴权、表单解析都算进来,
#: 迁进来的资产和别人手工上传的走的是同一条路。
BASE = "http://127.0.0.1:8000"

#: 目录 -> 门户里的类型。``knowledge/`` 与 ``rule/`` 不迁(前者留在 Gerrit 只做展示)。
PLAN: tuple[tuple[str, str], ...] = (("agent", "agent"), ("tool", "tool"), ("skill", "skill"))

#: 资产目录里这些是说明文件,不是资产。与 ``gerrit_assets.NON_ASSET_NAMES`` 一致。
NON_ASSET = frozenset({"INDEX.md", "README.md", "RULES.md"})

MANIFEST = "manifest.yaml"
SKILL_MD = "SKILL.md"


@dataclass
class Outcome:
    kind: str
    directory: str
    old_id: str
    action: str                 # migrated | skipped | failed | would-migrate
    reason: str = ""
    creator: str = ""
    category_id: str = ""
    new_id: str = ""
    launch: str = ""
    files: int = 0
    bytes: int = 0
    notes: list[str] = field(default_factory=list)


def _post(path: str, *, token: str, fields: dict[str, str],
          upload: tuple[str, bytes] | None = None) -> dict:
    """multipart POST。手写而不是引 requests:容器里没有那个包,而这里只需要一种编码。"""
    boundary = "----portal-migrate-boundary"
    parts: list[bytes] = []
    for k, v in fields.items():
        if v is None:
            continue
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
            f"{v}\r\n".encode()
        )
    if upload is not None:
        name, blob = upload
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{name}\"\r\nContent-Type: application/zip\r\n\r\n".encode()
        )
        parts.append(blob)
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        BASE + path, data=body, method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:800]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def _zip_of(tree: dict[str, bytes], top: str) -> tuple[bytes, int]:
    """把一个资产目录打成 zip,内容套在 ``top/`` 下。

    套一层顶层目录是因为两个上传端点都按"一个包一个顶层目录"理解归档
    (``_strip_archive_wrapper`` 只在恰好一个目录时剥)。不套的话文件会散在仓库根上。
    """
    buf = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in sorted(tree):
            data = tree[rel]
            total += len(data)
            zf.writestr(f"{top}/{rel}", data)
    return buf.getvalue(), total


def _manifest(ref: str, path: str) -> dict:
    try:
        return yaml.safe_load(gc.read_text(ref, f"{path}/{MANIFEST}")) or {}
    except Exception:
        return {}


def _text(v: object) -> str:
    return str(v).strip() if v not in (None, "") else ""


def _existing(db, kind: str, directory: str):
    """门户里对应的那一行。归属与分组从它读 —— 不重新解析 owner。"""
    old_id = f"gerrit-{kind}-{directory}"
    if kind == "skill":
        return old_id, db.get(RpAiSkill, old_id)
    return old_id, db.get(RpAiMarketItem, old_id)


def _migrate_skill(row: RpAiSkill, tree: dict[str, bytes], directory: str,
                   manifest: dict, *, apply: bool, out: Outcome) -> None:
    definition = tree.get(SKILL_MD)
    if definition is None:
        out.action, out.reason = "skipped", f"目录里没有 {SKILL_MD},不是技能"
        return
    # 附件单独打包:``instructions`` 是正文,``file`` 是作者原本那个目录。SKILL.md 也留在
    # 包里 —— 上传路径会剥掉外层目录后原样收下,少了它下载到的包缺正文。
    blob, total = _zip_of(tree, directory)
    out.files, out.bytes = len(tree), total
    out.launch = "—"
    if not apply:
        out.action = "would-migrate"
        return
    token, _ = create_token(row.uploader_eid or "", row.author or None)
    res = _post(
        "/api/skills/upload", token=token,
        fields={
            "eid": row.uploader_eid or "",
            "name": row.name or directory,
            "categoryId": row.category_id or "",
            "description": row.description or _text(manifest.get("description")) or None,
            "author": row.author or None,
            "tags": json.dumps(row.tags or []),
            "instructions": definition.decode("utf-8", "replace"),
        },
        upload=(f"{directory}.zip", blob),
    )
    # 新 id 在 `skill.id` 里(``SkillUploadResult.skill`` 是一个 SkillRead)。
    # **取不到就当失败**:对应表是搬下载量的唯一依据,一个空 id 会让这条资产的下载记录在
    # 删旧行那一步被 CASCADE 带走,而当时已经没有线索能把它找回来。第一次跑就是这么丢的
    # 3 条 —— 那时这里读的是 res["id"],静默得到 None、还照样报 migrated。
    skill = res.get("skill") or {}
    out.new_id = str(skill.get("id") or "")
    if not out.new_id:
        out.action = "failed"
        out.reason = (
            "上传成功了但响应里读不出新 id —— 没有它就搬不了下载量。"
            f"响应的键:{sorted(res)}"
        )
        return
    state = str(res.get("auditState") or "")
    out.action = "migrated"
    if state:
        out.notes.append(f"审计={state}")
    if res.get("mrUrl"):
        out.notes.append(f"复核 MR {res['mrUrl']}")


def _migrate_package(row: RpAiMarketItem, tree: dict[str, bytes], directory: str,
                     kind: str, manifest: dict, *, apply: bool, out: Outcome) -> None:
    blob, total = _zip_of(tree, directory)
    out.files, out.bytes = len(tree), total
    token, _ = create_token(row.creator or "", row.author or None)
    # 先体检,拿门户自己从包里推出来的启动命令。推不确定的不迁 —— 理由见模块头。
    ins = _post(
        "/api/market/agent-upload/inspect", token=token,
        fields={"itemType": kind}, upload=(f"{directory}.zip", blob),
    )
    guesses = ins.get("launchGuesses") or []
    certain = [g for g in guesses if g.get("certain")]
    if not certain:
        out.action = "skipped"
        out.reason = (
            "推不出确定的启动命令(结构化来源没命中)。"
            + (f"README 里的候选:{[g.get('command') for g in guesses][:3]}"
               if guesses else "包里也没有 README 候选。")
            + " 需要人给一条 launch 再单独迁。"
        )
        return
    out.launch = str(certain[0].get("command") or "")
    declared = (ins.get("declared") or {})
    if not apply:
        out.action = "would-migrate"
        return
    res = _post(
        "/api/market/agent-upload", token=token,
        fields={
            "eid": row.creator or "",
            "name": row.name or directory,
            "description": row.description or _text(manifest.get("description")) or None,
            "categoryId": row.category_id or "",
            "tags": json.dumps(row.tags or []),
            "launch": out.launch,
            "runtime": str(declared.get("runtime") or "oneshot"),
            "keep": json.dumps(declared.get("keep") or []),
            "prerequisites": json.dumps(declared.get("prerequisites") or []),
            "itemType": kind,
            "version": _text(manifest.get("version")) or "1.0.0",
        },
        upload=(f"{directory}.zip", blob),
    )
    out.new_id = str(res.get("id") or res.get("itemId") or "")
    if not out.new_id:
        # 同 skill 那侧:没有新 id 就搬不了下载量,而删旧行时那些记录会被 CASCADE 带走。
        out.action = "failed"
        out.reason = f"上传成功了但响应里读不出新 id。响应的键:{sorted(res)}"
        return
    out.action = "migrated"
    if res.get("repoUrl"):
        out.notes.append(str(res["repoUrl"]))


def run(*, apply: bool, only: set[str], map_path: Path | None) -> list[Outcome]:
    db = SessionLocal()
    ref = gc.head_ref()
    print(f"Gerrit ref = {ref[:12]}    模式 = {'APPLY(真写)' if apply else 'DRY-RUN(不写)'}")
    results: list[Outcome] = []
    for directory_name, kind in PLAN:
        for entry in gc.list_dir(ref, directory_name):
            name = str(entry["name"])
            if entry["type"] != "tree" or name in NON_ASSET:
                continue
            key = f"{directory_name}/{name}"
            if only and key not in only:
                continue
            old_id, row = _existing(db, kind, name)
            out = Outcome(kind=kind, directory=name, old_id=old_id, action="failed")
            results.append(out)
            if row is None:
                out.reason = "门户里没有对应的行 —— 它不是这次要迁的那一批"
                out.action = "skipped"
                continue
            out.creator = (getattr(row, "creator", None)
                           or getattr(row, "uploader_eid", None) or "")
            out.category_id = row.category_id or ""
            if not out.creator:
                out.reason = "这一行没有归属人,迁过去会变成执行者的 —— 先把归属定了"
                out.action = "skipped"
                continue
            try:
                tree = gc.read_tree(ref, key)
                manifest = _manifest(ref, key)
                if kind == "skill":
                    _migrate_skill(row, tree, name, manifest, apply=apply, out=out)
                else:
                    _migrate_package(row, tree, name, kind, manifest, apply=apply, out=out)
            except Exception as exc:                      # noqa: BLE001 - 一条失败不该带走整批
                out.action, out.reason = "failed", f"{type(exc).__name__}: {exc}"
    _report(results)
    if apply and map_path is not None:
        mapping = {o.old_id: o.new_id for o in results if o.action == "migrated" and o.new_id}
        map_path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n旧 id -> 新 id 的对应已写到 {map_path}({len(mapping)} 条)")
        print("搬下载量那一步按它改指向;没有这份对应就搬不了。")
    return results


def _report(results: list[Outcome]) -> None:
    print()
    print(f"{'类型':<6} {'目录':<30} {'结果':<14} {'归属':<9} {'分组':<14} 新 id / 原因")
    print("-" * 120)
    for o in sorted(results, key=lambda r: (r.kind, r.directory)):
        tail = o.new_id or o.reason
        print(f"{o.kind:<6} {o.directory:<30} {o.action:<14} {o.creator:<9} "
              f"{o.category_id:<14} {tail}")
    counts: dict[str, int] = {}
    for o in results:
        counts[o.action] = counts.get(o.action, 0) + 1
    print()
    print("合计:" + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    skipped = [o for o in results if o.action in ("skipped", "failed")]
    if skipped:
        print("\n没迁过去的(每条都要人看一眼,不要当成噪音):")
        for o in skipped:
            print(f"  {o.kind}/{o.directory}: {o.reason}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__ or "")
    ap.add_argument("--apply", action="store_true",
                    help="真的写。不给这个参数就是空跑,一个字节都不动。")
    ap.add_argument("--only", action="append", default=[],
                    help="只处理这些(形如 agent/ci-swat),可重复。")
    ap.add_argument("--map", type=Path, default=None,
                    help="把 旧 id -> 新 id 的对应写到这个文件。--apply 时必须给。")
    args = ap.parse_args(argv)
    if args.apply and args.map is None:
        # 没有这份对应,下载量就搬不回去,而那一步是用户明确要求的。宁可现在拒绝。
        print("--apply 必须同时给 --map:没有 旧->新 的对应,下载量搬不回去。", file=sys.stderr)
        return 2
    run(apply=args.apply, only=set(args.only), map_path=args.map)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
