# RTO/RPO Evidence - Lab 23

Moi so lieu ben duoi duoc truy vet tu log JSONL cua hai drill ngay 2026-10-09.

## 1. Drill 1 - Khong co DR

| Chi so | Gia tri | Cach do | Evidence |
|---|---:|---|---|
| t_outage | `2026-10-09T04:59:48` | chaos `SIGSTOP` Region A | `chaos/chaos-events.jsonl:1` |
| Request fail dau tien | `+0.0s` | request dau tien sau outage co `ok:false` | `reports/drill-1-nodr.jsonl:17` |
| Request thanh cong sau do | Khong co | tat ca request con lai deu fail | `reports/drill-1-nodr.jsonl:32` |
| RTO | `NO_RECOVERY` | ket qua do tu timestamp | `reports/measure-drill-1.json:25` |

## 2. Drill 2 - Co DR

| Moc | Giay tu t_outage | Cach do | Evidence |
|---|---:|---|---|
| t_outage | `0.0s` | chaos `SIGSTOP` Region A | `chaos/chaos-events.jsonl:3` |
| User thay loi dau tien | `+0.4s` | request dau tien co `ok:false` | `reports/drill-2-withdr.jsonl:26` |
| Health check phat hien | `+15.2s` | Region A chuyen `UNHEALTHY` sau 3 fail | `reports/health-events.jsonl:2` |
| Snapshot restore xong | `+15.5s` | restore dat vector va model tai Region B | `reports/failover-events.jsonl:2` |
| Region B ready | `+21.6s` | `/readyz` thanh cong sau warm-up | `reports/failover-events.jsonl:4` |
| DNS cutover | `+21.7s` | active region chuyen sang B | `reports/failover-events.jsonl:5` |
| RTO do duoc | `23.3s` | request dau tien thanh cong tu Region B | `reports/drill-2-withdr.jsonl:37` |

| Chi so | Do duoc | Muc tieu | Verdict |
|---|---:|---:|---|
| RTO - Inference API | `23.3s` | `300s` | PASS |
| RPO - Vector DB | `2.0s` / `1` document | `300s` | PASS |

Ket qua tong hop doc lap xac nhan `valid:true`, khong warning va Region B phuc hoi
traffic tai `reports/measure-drill-2.json:2`, `reports/measure-drill-2.json:4` va
`reports/measure-drill-2.json:6`. RPO va document mat duoc ghi truc tiep tai
`reports/failover-events.jsonl:2`.

## 3. Thanh phan RTO

| Thanh phan | Giay | Nguon timestamp | Cach giam |
|---|---:|---|---|
| Health-check detection floor | `15.0s` configured; `15.152s` observed | `interval_s=5.0 x threshold=3` va detection tai `reports/health-events.jsonl:2`; outage tai `chaos/chaos-events.jsonl:3` | Giam interval, doi lai tang tai probe va nguy co flapping |
| Verify va snapshot restore | `0.330s` | detect `reports/health-events.jsonl:2` den scale `reports/failover-events.jsonl:3` | Snapshot nho hon, storage nhanh hon |
| GPU warm-up va cutover | `6.173s` | scale `reports/failover-events.jsonl:3` den cutover `reports/failover-events.jsonl:5`; `waited_s=6.108` tai `reports/failover-events.jsonl:4` | Duy tri warm capacity |
| DNS/LB TTL va request ke tiep | `1.690s` | cutover `reports/failover-events.jsonl:5` den recovery `reports/drill-2-withdr.jsonl:37` | Giam TTL hoac tang tan suat retry co backoff |

Bon latency quan sat (`15.152 + 0.330 + 6.173 + 1.690`) tong thanh `23.345s`,
lam tron mot chu so thap phan thanh RTO `23.3s`. Detection floor cau hinh van la
`15.0s`; observed latency co the lech nho theo pha polling va do chinh xac timestamp.
