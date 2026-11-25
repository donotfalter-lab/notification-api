# notification-api

A multi-tenant notification API that sends email asynchronously.

Built with FastAPI, async SQLAlchemy (PostgreSQL), and Redis as a queue. A background
worker delivers email; Mailpit catches it locally. Metrics go to Prometheus and
Grafana, and load tests use k6. Everything runs with Docker Compose.

## Getting started

Requires Docker with Docker Compose.

1. Create your local config (once), then set a real `ADMIN_TOKEN` in `.env`:

   ```bash
   cp .env.example .env
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # paste as ADMIN_TOKEN
   ```

   If you already have a `.env` from an earlier phase, compare it with
   `.env.example` and add any new variables (`REDIS_URL`, `ADMIN_TOKEN`, ...).

2. Build and start everything. The API waits for Postgres, applies database
   migrations, then serves on port 8000 with auto-reload:

   ```bash
   docker compose up --build
   ```

   Re-run with `--build` whenever `Dockerfile` or `requirements.txt` changes.

3. In a second terminal, load demo data: 5 tenants, 2,000 customers each (all
   `@example.com` addresses), and one API key per tenant. The keys are printed
   and saved to `loadtests/keys.json` (gitignored). Safe to re-run; existing keys
   are reused:

   ```bash
   docker compose exec api python scripts/seed.py
   ```

4. Try it. `/v1` endpoints need a tenant API key in the `X-API-Key` header and
   only ever return that tenant's data:

   ```bash
   curl localhost:8000/health
   curl -H "X-API-Key: ntf_live_..." "localhost:8000/v1/customers?limit=5&offset=0"
   ```

   Interactive API docs: http://localhost:8000/docs (use **Authorize** to set a key).

## Admin endpoints

Protected by `Authorization: Bearer $ADMIN_TOKEN`:

| Method | Path | What it does |
| --- | --- | --- |
| POST | `/admin/tenants` | Create a tenant: `{"name": "...", "plan": "free" \| "pro"}` |
| POST | `/admin/tenants/{id}/api-keys` | Create a key. The raw key is in the response **once only**; only its hash is stored. |
| POST | `/admin/api-keys/{id}/revoke` | Revoke a key. It stops working immediately. |

```bash
curl -X POST localhost:8000/admin/tenants \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "wayne", "plan": "pro"}'
```

## Running tests

```bash
docker compose exec api pytest
```

Database tests use a separate `<POSTGRES_DB>_test` database (created automatically)
and are skipped if Postgres isn't reachable.

## Stopping

```bash
docker compose down        # stop containers, keep data
docker compose down -v     # stop and delete the database volume
```
