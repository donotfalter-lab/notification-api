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
   `.env.example` and add any new variables (`REDIS_URL`, `ADMIN_TOKEN`,
   `SMTP_*`, `WORKER_*`, ...).

2. Build and start everything. Services: `postgres`, `redis`, `mailpit`, `api`
   (applies database migrations, then serves on port 8000 with auto-reload) and
   `worker` (sends the emails):

   ```bash
   docker compose up --build
   ```

   Re-run with `--build` whenever `Dockerfile` or `requirements.txt` changes.
   The worker doesn't auto-reload: after changing `worker/` code, run
   `docker compose restart worker`.

3. In a second terminal, load demo data: 5 tenants, 2,000 customers each (all
   `@example.com` addresses), and one API key per tenant. The keys are printed
   and saved to `loadtests/keys.json` (gitignored). Safe to re-run; existing keys
   are reused:

   ```bash
   docker compose exec api python -m scripts.seed
   ```

   (`python scripts/seed.py` works too.)

4. Try it. `/v1` endpoints need a tenant API key in the `X-API-Key` header and
   only ever return that tenant's data:

   ```bash
   curl localhost:8000/health
   curl -H "X-API-Key: ntf_live_..." "localhost:8000/v1/customers?limit=5&offset=0"
   ```

   Interactive API docs: http://localhost:8000/docs (use **Authorize** to set a key).

5. Send an email. The API saves it, queues it, and answers `202` right away; the
   worker delivers it a moment later. Open **Mailpit at http://localhost:8025**
   to see it arrive:

   ```bash
   curl -X POST localhost:8000/v1/notifications \
     -H "X-API-Key: ntf_live_..." -H "Content-Type: application/json" \
     -H "Idempotency-Key: order-1234-shipped" \
     -d '{"customer_id": "<id from /v1/customers>", "subject": "Your order shipped", "body": "It is on the way."}'
   ```

## Notifications

| Method | Path | What it does |
| --- | --- | --- |
| POST | `/v1/notifications` | Queue one email: `{customer_id, subject, body}`. Returns `202 {id, status}`. |
| POST | `/v1/notifications/batch` | Queue up to 100: `{"notifications": [...]}`. All-or-nothing. |
| GET | `/v1/notifications/{id}` | Status (`queued`, `sent`, `failed`), attempts, last error. |

- **Idempotency:** send an `Idempotency-Key` header and repeating the request
  (same key, same tenant) returns the original notification instead of creating
  a duplicate. The repeat response carries `Idempotent-Replayed: true`.
- The customer must belong to your tenant, otherwise `422 Customer not found`.
- If Redis is down, the API answers `503` and saves nothing, so retrying is safe.

## Rate limits and quotas

Every `/v1` request is limited per tenant, based on the tenant's plan. Change
the numbers in `.env` (`RATE_LIMIT_*`, `DAILY_EMAIL_QUOTA_*`).

| Plan | Requests | Burst | Emails per day |
| --- | --- | --- | --- |
| free | 10/s | 20 | 5,000 |
| pro | 100/s | 200 | 100,000 |

- **Token bucket:** each request spends a token. The bucket holds up to *burst*
  tokens and refills at *requests/s*. Check and spend happen in one Lua script in
  Redis, so the limit holds across any number of API containers.
- **Headers on every `/v1` response:** `X-RateLimit-Limit` (bucket size),
  `X-RateLimit-Remaining` (tokens left), `X-RateLimit-Reset` (seconds until the
  bucket is full again).
- **Over the limit:** `429 {"error": "...", "retry_after_seconds": 0.1}` plus a
  `Retry-After` header (whole seconds).
- **Daily email quota:** counts emails, not requests (a batch of 50 uses 50) and
  resets at midnight UTC. A batch that doesn't fit is rejected whole. Repeated
  `Idempotency-Key` requests and rejected requests don't use quota.
- **If Redis is down,** limits are not enforced (fail open) and the API logs a
  warning, so an outage of the limiter doesn't take the API down with it.

## The email worker

`worker/` runs as its own container (same image, `python -m worker.main`):

- **Reliable queue:** it claims a job with `BLMOVE` from `notifications:queue`
  into its own processing list and only removes it once the job is finished. If a
  worker crashes, its heartbeat expires (30s) and another worker puts its jobs back.
- **Concurrency:** up to `WORKER_CONCURRENCY` (default 20) sends at once.
- **Retries:** a failed send is retried after 1s, 2s, 4s, 8s (`WORKER_RETRY_BASE_SECONDS`,
  with jitter) using a Redis sorted set. After `WORKER_MAX_ATTEMPTS` (5) the
  notification is marked `failed` and moved to the dead-letter list.
- **Fake provider:** `SMTP_DELAY_MS` adds random latency and `SMTP_FAIL_RATE`
  (default 2%) fails sends on purpose, so you can watch retries in the logs.
- **Graceful shutdown:** on `docker compose stop worker` it stops taking jobs and
  finishes the ones in flight first.
- **Delivery is at-least-once:** if a worker dies between sending and recording
  the send, that email is sent again after recovery.

```bash
docker compose logs -f worker              # picked up / sent / retry / failed lines
docker compose up -d --scale worker=3      # more workers share the same queue
```

## Admin endpoints

Protected by `Authorization: Bearer $ADMIN_TOKEN`:

| Method | Path | What it does |
| --- | --- | --- |
| POST | `/admin/tenants` | Create a tenant: `{"name": "...", "plan": "free" \| "pro"}` |
| POST | `/admin/tenants/{id}/api-keys` | Create a key. The raw key is in the response **once only**; only its hash is stored. |
| POST | `/admin/api-keys/{id}/revoke` | Revoke a key. It stops working immediately. |
| GET | `/admin/queue/stats` | Counts: `queued`, `processing`, `delayed`, `dead_letter`, `workers`. |
| POST | `/admin/dead-letter/replay` | Put every dead-lettered notification back on the queue with fresh attempts. |

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
