"""Reconcile the audit projection with the review MRs.

Two triggers, one convergence function:

    python -m app.reconcile                 one sweep, then exit (cron, or by hand)
    python -m app.reconcile --watch         sweep every RECONCILE_INTERVAL seconds
    POST /api/audit/reconcile               webhook, for latency

The periodic sweep is what makes it correct: webhooks are lossy, so a system that
only reacts to events drifts silently. The webhook only shortens the delay.

这个循环里还顺带做接口报错的 LLM 诊断（``--watch`` 模式下才做）。搭在这里而不是另起一个
容器：生产上这个进程的 entrypoint 已经是 ``python -m app.reconcile --watch``，挂进来就
不用改 compose、不用动 platform-ci。两件事互不相干，所以任一方失败都不许影响另一方 ——
下面各自包了 try。
"""
import argparse
import logging
import os
import time

from app.config import get_settings
from app.database import SessionLocal
from app.services.api_errors import diagnose_pending
from app.services.skill_audit.reconcile import reconcile_all

log = logging.getLogger("skill_audit.reconcile")


def sweep() -> dict:
    with SessionLocal() as db:
        return reconcile_all(db)


def diagnose_sweep() -> dict:
    """给还没有结论的接口报错出诊断。一轮最多几条、涌入时只做 1 条，见 services/api_errors。"""
    cfg = get_settings()
    with SessionLocal() as db:
        return diagnose_pending(
            db,
            limit=cfg.error_diagnosis_batch,
            flood_threshold=cfg.error_diagnosis_flood_threshold,
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", help="keep sweeping")
    ap.add_argument("--interval", type=int,
                    default=int(os.environ.get("RECONCILE_INTERVAL", "300")),
                    help="seconds between sweeps in --watch mode")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if not args.watch:
        result = sweep()
        print(f"examined={result['examined']} changed={result['changed']}")
        return

    log.info("reconciling every %ds", args.interval)
    while True:
        try:
            result = sweep()
            if result["changed"]:
                log.info("changed: %s", result["changed"])
        except Exception:  # noqa: BLE001 - a sweep failure must not end the loop
            log.exception("sweep failed; retrying next interval")
        try:
            diag = diagnose_sweep()
            if diag["examined"]:
                log.info("api-error diagnosis: %s", diag)
        except Exception:  # noqa: BLE001 - 诊断失败同样不许结束循环，也不许影响上面的收敛
            log.exception("api-error diagnosis failed; retrying next interval")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
