"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import datetime
import json
import math
import os
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Ghi một bước runbook có timestamp vào JSONL."""
    ts = time.time()
    event = {
        "ts": ts,
        "iso": datetime.datetime.fromtimestamp(
            ts, datetime.timezone.utc).isoformat(),
        "step": n,
        "name": name,
        **kw,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(event) + "\n")
    print(json.dumps(event))
    return event


def confirm(auto: bool, msg: str) -> bool:
    """Yêu cầu xác nhận rõ ràng trừ khi đang chạy automation."""
    if auto:
        return True
    return input(f"{msg} [y/N] ").strip().lower() in {"y", "yes"}


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Chạy runbook bán tự động và trả về summary có thể dùng cho postmortem."""
    if primary not in URL or target not in URL or primary == target:
        raise ValueError("primary and target must be different known regions")

    started = time.time()
    failures = []
    target_alive_checks = 0
    for attempt in range(3):
        ready, reason = health_checker.probe(primary, timeout=1.0)
        if ready:
            failures = []
        else:
            failures.append(reason)
        try:
            response = httpx.get(f"{URL[target]}/healthz", timeout=1.0)
            if response.status_code == 200 and response.json().get("alive") is True:
                target_alive_checks += 1
        except Exception:
            pass
        if attempt < 2:
            time.sleep(5.0)
    target_alive = target_alive_checks == 3
    outage_confirmed = len(failures) == 3 and target_alive
    step(1, "xac_nhan_outage", primary=primary, target=target,
         confirmed=outage_confirmed, consecutive_fails=len(failures), reasons=failures,
         target_alive=target_alive, target_alive_checks=target_alive_checks)
    if not outage_confirmed:
        error = "target_not_alive" if not target_alive else "outage_not_confirmed"
        return {"ok": False, "failed_step": 1, "error": error}

    outage_ts = None
    chaos_log = pathlib.Path("chaos/chaos-events.jsonl")
    if chaos_log.exists():
        events = [json.loads(line) for line in chaos_log.read_text(encoding="utf-8").splitlines()
                  if line.strip()]
        kills = [event for event in events
                 if event.get("action") == "kill" and event.get("region") == primary
                 and isinstance(event.get("ts"), (int, float))
                 and 0 <= started - event["ts"] <= 300]
        outage_ts = kills[-1].get("ts") if kills else None

    # Give the separately running checker a short chance to publish its transition.
    # If absent, the runbook records its own three-probe decision so it also works standalone.
    detect_deadline = time.monotonic() + 5.0
    detected_ts = None
    health_log = pathlib.Path("reports/health-events.jsonl")
    while time.monotonic() < detect_deadline and detected_ts is None:
        if health_log.exists():
            try:
                events = [json.loads(line)
                          for line in health_log.read_text(encoding="utf-8").splitlines()
                          if line.strip()]
                detections = [event for event in events
                              if event.get("event") == "state_change"
                              and event.get("region") == primary
                              and event.get("to") == "UNHEALTHY"
                              and event.get("ts", 0) >= (outage_ts or started)]
                detected_ts = detections[0].get("ts") if detections else None
            except (json.JSONDecodeError, OSError):
                pass
        if detected_ts is None:
            time.sleep(0.25)

    if detected_ts is None:
        detected_ts = time.time()
        event = {
            "event": "state_change",
            "ts": detected_ts,
            "region": primary,
            "to": "UNHEALTHY",
            "reason": failures[-1],
            "consecutive_fails": len(failures),
            "interval_s": 5.0,
            "threshold": 3,
            "source": "runbook",
        }
        health_log.parent.mkdir(parents=True, exist_ok=True)
        with health_log.open("a", encoding="utf-8") as log:
            log.write(json.dumps(event) + "\n")

    alerted_at = time.time()
    approved = confirm(auto, f"Region {primary} is unavailable. Fail over to {target}?")
    step(2, "thong_bao_incident", primary=primary, target=target,
         confirmed=approved, outage_ts=outage_ts, detected_ts=detected_ts,
         notification_ts=alerted_at, approved_at=time.time())
    if not approved:
        return {"ok": False, "failed_step": 2, "error": "operator_cancelled"}

    failover_result = fo.failover(target, backend, wait=60.0)
    step(3, "scale_gpu_pool", target=target, ok=failover_result.get("ok", False),
         failover_elapsed_s=failover_result.get("elapsed_s"),
         failed_step=failover_result.get("failed_step"))
    if not failover_result.get("ok"):
        return {"ok": False, "failed_step": 3, "failover": failover_result}

    ready_state = failover_result.get("ready_state", {})
    vectors = ready_state.get("vectors", {})
    target_after = failover_result.get("target_after", {})
    step(4, "verify_state_replica", target=target,
         vector_count=vectors.get("count"), weights=target_after.get("weights"),
         rpo_seconds=failover_result.get("rpo", {}).get("rpo_seconds"),
         docs_lost=failover_result.get("rpo", {}).get("docs_lost"))

    active_region = pathlib.Path("edge/active_region").read_text(encoding="utf-8").strip()
    cutover_ok = active_region == target
    step(5, "dns_cutover", target=target, ok=cutover_ok,
         active_region=active_region)
    if not cutover_ok:
        return {"ok": False, "failed_step": 5, "failover": failover_result}

    edge_url = os.environ.get("EDGE_URL", "http://127.0.0.1:8080")
    latencies = []
    errors = 0
    with httpx.Client(timeout=3.0) as client:
        recovery_deadline = time.monotonic() + 15.0
        while time.monotonic() < recovery_deadline:
            try:
                response = client.get(f"{edge_url}/v1/infer", params={"q": "recovery probe"})
                body = response.json()
                if response.status_code == 200 and body.get("region") == target:
                    break
            except Exception:
                pass
            time.sleep(0.25)
        for request_no in range(10):
            request_started = time.monotonic()
            try:
                response = client.get(
                    f"{edge_url}/v1/infer", params={"q": f"golden signal {request_no}"})
                body = response.json()
                if response.status_code != 200 or body.get("region") != target:
                    errors += 1
            except Exception:
                errors += 1
            latencies.append((time.monotonic() - request_started) * 1000)

    ordered = sorted(latencies)
    p95 = ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]
    error_rate = errors / len(latencies)
    golden_ok = errors == 0
    step(6, "verify_golden_signals", target=target, ok=golden_ok,
         requests=len(latencies), p95_latency_ms=round(p95, 1),
         error_rate=round(error_rate, 3))

    elapsed = round(time.time() - started, 3)
    measure_command = (
        "python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl "
        "--target-rto 300"
    )
    step(7, "post_incident", ok=golden_ok, elapsed_s=elapsed,
         measure_command=measure_command)
    return {
        "ok": golden_ok,
        "primary": primary,
        "target": target,
        "elapsed_s": elapsed,
        "golden_signals": {
            "requests": len(latencies),
            "p95_latency_ms": round(p95, 1),
            "error_rate": round(error_rate, 3),
        },
        "failover": failover_result,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    result = run(a.primary, a.target, a.backend, a.auto)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result.get("ok") else 1)
