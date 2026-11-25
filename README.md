# notification-api

A multi-tenant notification API that sends email asynchronously.

Built with FastAPI, async SQLAlchemy (PostgreSQL), and Redis as a queue. A background
worker delivers email; Mailpit catches it locally. Metrics go to Prometheus and
Grafana, and load tests use k6. Everything runs with Docker Compose.

## Getting started

_Setup instructions coming soon._

```bash
cp .env.example .env
docker compose up
```
