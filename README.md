# notification-api

A multi-tenant notification API that sends email asynchronously.

Built with FastAPI, async SQLAlchemy (PostgreSQL), and Redis as a queue. A background
worker delivers email; Mailpit catches it locally. Metrics go to Prometheus and
Grafana, and load tests use k6. Everything runs with Docker Compose.

## Getting started

Requires Docker with Docker Compose.

1. Create your local config (once). Edit `.env` if you want a different password:

   ```bash
   cp .env.example .env
   ```

2. Build and start everything. The API waits for Postgres, applies database
   migrations, then serves on port 8000 with auto-reload:

   ```bash
   docker compose up --build
   ```

   Re-run with `--build` whenever `Dockerfile` or `requirements.txt` changes.

3. In a second terminal, load demo data (5 tenants, 2,000 customers each, all
   `@example.com` addresses). Safe to re-run:

   ```bash
   docker compose exec api python scripts/seed.py
   ```

4. Try it:

   ```bash
   curl localhost:8000/health
   curl "localhost:8000/v1/customers?limit=5&offset=0"
   ```

   Interactive API docs: http://localhost:8000/docs

## Running tests

```bash
docker compose exec api pytest
```

## Stopping

```bash
docker compose down        # stop containers, keep data
docker compose down -v     # stop and delete the database volume
```
