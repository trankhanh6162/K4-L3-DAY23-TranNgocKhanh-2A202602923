"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import datetime
import json
import os
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append một sự kiện có timestamp vào failover log và stdout."""
    ts = time.time()
    event = {
        "ts": ts,
        "iso": datetime.datetime.fromtimestamp(
            ts, datetime.timezone.utc).isoformat(),
        **kw,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(event) + "\n")
    print(json.dumps(event))
    return event


def state_of(region: str) -> dict:
    response = httpx.get(f"{URL[region]}/v1/state", timeout=2.0)
    response.raise_for_status()
    return response.json()


def failover(target: str, backend: str, wait: float) -> dict:
    """Restore và cutover sang target; mọi lỗi trước bước 5 đều abort an toàn."""
    if target not in URL:
        raise ValueError(f"unknown target region: {target}")
    if wait <= 0:
        raise ValueError("wait must be positive")

    started = time.monotonic()
    active = pathlib.Path("edge/active_region")
    source = active.read_text(encoding="utf-8").strip() if active.exists() else \
        ("b" if target == "a" else "a")
    result = {"ok": False, "target": target, "source": source}
    current_step = "1_verify_target"

    try:
        if source not in URL:
            raise ValueError(f"invalid active region: {source!r}")
        if source == target:
            raise ValueError(f"region-{target} is already active")

        target_before = state_of(target)
        result["target_before"] = target_before
        emit(step=current_step, target=target, ok=True,
             pool_state=target_before.get("pool_state"),
             weights=target_before.get("weights"), count=target_before.get("count"))

        current_step = "2_restore_snapshot"
        restored = snapshot.get(target, backend)
        rpo = snapshot.rpo(
            pathlib.Path(f"state/region-{source}/vectors.sqlite"),
            pathlib.Path(f"state/region-{target}/vectors.sqlite"),
        )
        result.update(restore=restored, rpo=rpo)
        emit(step=current_step, target=target, ok=True,
             rpo_seconds=rpo["rpo_seconds"], docs_lost=rpo["docs_lost"],
             embed_model_version=restored.get("embed_model_version"),
             snapshot_at=restored.get("snapshot_at"))

        current_step = "3_scale_pool"
        pool_file = pathlib.Path(f"state/region-{target}/pool_state")
        pool_file.parent.mkdir(parents=True, exist_ok=True)
        pool_file.write_text("full\n", encoding="utf-8")
        emit(step=current_step, target=target, ok=True, pool_state="full")

        current_step = "4_wait_ready"
        deadline = time.monotonic() + wait
        ready_state = None
        last_reason = "not_probed"
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"{URL[target]}/readyz", timeout=min(2.0, wait))
                ready_state = response.json()
                if response.status_code == 200 and ready_state.get("ready") is True:
                    break
                last_reason = ",".join(ready_state.get("reasons") or []) or \
                    f"http_{response.status_code}"
            except Exception as exc:
                last_reason = type(exc).__name__
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
        else:
            ready_state = None

        waited = round(time.monotonic() - (deadline - wait), 3)
        if ready_state is None or ready_state.get("ready") is not True:
            emit(step=current_step, target=target, ok=False,
                 waited_s=waited, reason=last_reason)
            result.update(failed_step=current_step, error=last_reason,
                          elapsed_s=round(time.monotonic() - started, 3))
            return result

        result["ready_state"] = ready_state
        target_after = state_of(target)
        result["target_after"] = target_after
        emit(step=current_step, target=target, ok=True, waited_s=waited,
             count=target_after.get("count"), weights=target_after.get("weights"))

        current_step = "5_dns_cutover"
        temp = active.with_name(f"{active.name}.{os.getpid()}.tmp")
        temp.write_text(target, encoding="utf-8")
        temp.replace(active)
        emit(step=current_step, target=target, ok=True, active_region=target)

        result.update(ok=True, active_region=target,
                      elapsed_s=round(time.monotonic() - started, 3))
        return result
    except (Exception, SystemExit) as exc:
        error = str(exc) or type(exc).__name__
        emit(event="failover_aborted", failed_step=current_step,
             target=target, ok=False, error=error)
        result.update(failed_step=current_step, error=error,
                      elapsed_s=round(time.monotonic() - started, 3))
        return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    result = failover(a.target, a.backend, a.wait)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result.get("ok") else 1)
