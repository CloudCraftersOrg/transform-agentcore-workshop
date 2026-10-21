# {{service_name}}

Containerized microservice migrated from the legacy Flask monolith (12-factor, Flask 3, SQLAlchemy Core, gunicorn).

| What | Where |
|---|---|
| JSON API (contract unchanged) | `app/api.py` |
| Liveness / readiness | `{{health_path}}` / `{{readiness_path}}` (`app/health.py`) |
| Settings (env only) | `DATABASE_URL` (`sqlite:///…` locally, `postgresql://…` on RDS), `ADMIN_TOKEN`, `LOG_LEVEL`, `SEED_DEMO_DATA` |
| Schema migration | `python -m app.migrate` (also on gunicorn start unless `DB_AUTO_MIGRATE=false`) |
| Nightly report (CronJob) | `python -m app.report` → CSV on stdout, or S3 with `REPORT_S3_URI` |

```bash
pip install -r requirements-dev.txt && pytest -q                     # tests (SQLite)
TEST_DATABASE_URL=postgresql://user:pass@localhost/app pytest -q   # same tests on PostgreSQL
docker build -t {{image}}:dev . && docker run --rm -p {{port}}:{{port}} -e SEED_DEMO_DATA=true {{image}}:dev
kubectl apply -k ../k8s/overlays/local                              # kind (after: kind load docker-image {{image}}:dev)
```
