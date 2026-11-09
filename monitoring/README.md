# OPD Wallet monitoring (Prometheus + Grafana)

## 1. Install and start
```
uv add prometheus-client            # or: pip install prometheus-client
cd monitoring
docker compose up -d
```
* Grafana    http://localhost:3000   login admin / admin   (dashboard "OPD Wallet - Operations, AI Guardrails and Claims" opens by default)
* Prometheus http://localhost:9090   (Status -> Targets must show opd-api and opd-worker as UP; Alerts shows the rules)

## 2. API (already wired in app/main.py)
Start it as usual (`uv run python -m app.main`). Check http://localhost:8000/metrics prints lines starting with `opd_`.

## 3. Worker (3 small edits in your running claims_worker / Kafka consumer file)
```python
from app.monitoring import metrics

# once, at start-up of the worker process:
metrics.start_worker_metrics_server(8001)

# when a claim event is received:
metrics.worker_event("received")

# after "DB COMMIT SUCCESS":
metrics.worker_event("saved")

# inside the `except` that prints "DATABASE ERROR":
metrics.db_error("write_claim", e)
metrics.worker_event("failed")
```
The bill reading metrics (extractions, audit score, LLM calls) are already recorded by extraction_service.py, which runs in the worker.
If the worker runs inside the API process, skip `start_worker_metrics_server` and remove the opd-worker job.

## 4. Linux
`host.docker.internal` is mapped in docker-compose.yml. If the API runs in another container, change the targets in prometheus/prometheus.yml.

## 5. Alerts
Rules are in prometheus/alerts.yml. Add Alertmanager (email/Slack) later; until then they are visible in Prometheus and Grafana.
