# Postmortem - DR Drill Lab 23

## 1. Timeline

| ISO time | Su kien | Evidence |
|---|---|---|
| 2026-10-09T05:40:14.528+00:00 | Region A bi netblock | `chaos/chaos-events.jsonl:3` |
| 2026-10-09T05:40:14.880+00:00 | User dau tien nhan loi | `reports/drill-2-withdr.jsonl:26` |
| 2026-10-09T05:40:29.679+00:00 | Health checker danh dau A `UNHEALTHY` | `reports/health-events.jsonl:2` |
| 2026-10-09T05:40:29.846+00:00 | Operator/automation xac nhan failover | `reports/runbook-run.jsonl:2` |
| 2026-10-09T05:40:36.183+00:00 | Region B ready va DNS cutover | `reports/failover-events.jsonl:4`, `reports/failover-events.jsonl:5` |
| 2026-10-09T05:40:37.873+00:00 | Request dau tien thanh cong tu Region B | `reports/drill-2-withdr.jsonl:37` |

## 2. RTO/RPO Va Gap

- RTO target: `300s`; measured: `23.3s`; headroom/gap: `276.7s`.
- RPO target: `300s`; measured: `2.0s` va `1` document mat; headroom/gap: `298.0s`.
- Buoc ton nhieu thoi gian nhat la health-check detection: `15.2s`, chiem khoang
  `65.2%` RTO. Detection tai `reports/measure-drill-2.json:11`, RTO tai
  `reports/measure-drill-2.json:20`.

## 3. Root Cause - 5 Whys

1. User gap loi vi Edge van route toi Region A sau khi process bi pause.
2. Edge khong tu cutover vi active region la control-plane pointer co chu dich.
3. Pointer chi duoc doi sau khi health checker du ba failure lien tiep.
4. Nguong ba lan ngan mot loi probe don le gay failover/flapping.
5. Region B can restore state va warm-up truoc cutover de tranh outage kep.

Ket luan: outage la tinh huong duoc inject; phan lon RTO den tu chinh sach detection
chong flapping, khong phai thao tac restore hay loi ca nhan.

## 4. Action Items

| # | Action item | Owner | Deadline | Tac dong du kien |
|---|---|---|---|---|
| 1 | Chay health probes song song va canh bao khi effective interval vuot 5s | SRE | 2026-10-16 | Giam jitter detection 1-2s |
| 2 | Thu nghiem interval 2s voi circuit breaker va success threshold | AI Platform | 2026-10-23 | Giam detection floor tu 15s xuong 6s |
| 3 | Dung SQLite backup API cho snapshot nhat quan | Data Platform | 2026-10-30 | Giam rui ro restore loi/data corruption |

## 5. Cau Hoi Bat Buoc

1. `interval x threshold = 5s x 3 = 15s`, chiem `64.4%` RTO `23.3s`.
2. Neu interval la 1s, detection floor con 3s, ly thuyet giam khoang 12s. Doi lai
   he thong tang 5 lan tan suat probe va de nhay cam hon voi latency spike; can
   hysteresis/circuit breaker de tranh flapping.
3. `docs_lost=1` la mot document duoc ghi tai primary sau snapshot cuoi va khong co
   trong ban restore. Neu outage keo dai 6 gio va primary mat vinh vien, day la du
   lieu khach hang khong the truy van tai Region B va phai duoc tai tao/reconcile tu
   source-of-truth, khong chi la mot con so lag ky thuat.
