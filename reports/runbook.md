# Runbook - Region chinh down

Pham vi: bare-mode trong WSL, primary Region A, target Region B, snapshot filesystem.
Khong cutover neu Region B chua tra HTTP 200 tai `/readyz`.

Truoc drill, chay health checker trong terminal rieng theo dung GUIDE:

```bash
python3 dr/health_checker.py --interval 5 --threshold 3 --duration 100 \
  --out reports/health-events.jsonl
```

`dr/runbook.py` van tu xac nhan bang ba probe va ghi detection fallback neu checker
tren khong chay, nhung drill cham diem phai chay ca hai de co timeline tai lap duoc.

| # | Buoc | Lenh copy-paste | Hoan thanh khi | Owner |
|---|---|---|---|---|
| 1 | Xac nhan outage va survivor | `for i in 1 2 3; do python3 chaos/kill_region.py status; sleep 5; done` | Region A fail 3 lan lien tiep; Region B van `alive:true` | On-call SRE |
| 2 | Mo incident va xin phe duyet | `python3 dr/runbook.py --primary a --target b --backend fs` | Incident Commander doc prompt va nhap `y`; `reports/runbook-run.jsonl` co step 1-2 | Incident Commander |
| 3 | Xac minh restore va scale | `tail -n 5 reports/failover-events.jsonl` | Co dung thu tu `1_verify_target`, `2_restore_snapshot`, `3_scale_pool`, `4_wait_ready`, `5_dns_cutover`, tat ca `ok:true` | On-call SRE |
| 4 | Verify state replica | `curl http://127.0.0.1:8002/v1/state` | `weights:true`, `count>0`, `pool_state:"full"` | AI Platform |
| 5 | Verify DNS/LB cutover | `curl http://127.0.0.1:8080/edge/state` | `active_region:"b"`; cutover sau `4_wait_ready` | Incident Commander |
| 6 | Verify golden signals | `curl "http://127.0.0.1:8080/v1/infer?q=golden%20signal" && tail -n 2 reports/runbook-run.jsonl` | Response 200 tu B; step 6 co 10 requests, error rate `0.0`, p95 `71.4ms` tai `reports/runbook-run.jsonl:6` | Service Owner |
| 7 | Do RTO/RPO va dong incident | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid:true`, `warnings:[]`, `rto_verdict:"PASS"` | Incident Commander |

## Abort

Abort truoc cutover neu snapshot khong ton tai, restore loi, model weights thieu,
vector count bang 0, Region B khong alive, hoac Region B khong ready trong 60 giay.
Khong sua `edge/active_region` bang tay.

## Failback B Sang A

Chi failback khi A tra `/readyz` HTTP 200 ba lan, state moi nhat da duoc snapshot tu
B, va error rate tai B vuot 1% trong 5 phut hoac co loi toan ven du lieu. Incident
Commander la nguoi duy nhat phe duyet.

```bash
# 1. Resume A neu drill dung netblock, sau do verify readiness 3 lan.
python3 chaos/kill_region.py restore --region a --backend bare
for i in 1 2 3; do curl -f http://127.0.0.1:8001/readyz; sleep 5; done

# 2. Snapshot state moi nhat tu active Region B, roi failover co kiem soat ve A.
python3 state/snapshot.py put --region b --backend fs
python3 dr/failover.py --target a --backend fs --wait 60
curl http://127.0.0.1:8080/edge/state

# 3. Verify 10 golden requests deu duoc Region A phuc vu.
for i in $(seq 1 10); do
  curl -fsS "http://127.0.0.1:8080/v1/infer?q=failback-$i" | grep -q '"region":"a"' || exit 1
done
```

Failback hoan thanh khi failover log co du nam buoc moi cho target A, Edge tra
`active_region:"a"`, va 10 golden requests qua Edge deu duoc serve boi A.
