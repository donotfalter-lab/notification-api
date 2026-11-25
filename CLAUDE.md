# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

A multi-tenant notification API that sends email asynchronously. Clients call the
HTTP API to request a notification; the API records it and enqueues it, and a
separate background worker delivers the email.

## Stack

- Python 3.12
- FastAPI
- SQLAlchemy 2.0 (async) with asyncpg (PostgreSQL)
- Alembic for migrations
- Redis via redis-py asyncio (queue)
- Pydantic v2 + pydantic-settings
- Mailpit as the fake SMTP server (local/dev)
- Prometheus + Grafana for metrics
- k6 for load tests
- Docker Compose for running everything

## Layout

```
app/              FastAPI app
  main.py         app factory / entrypoint
  config.py       settings (pydantic-settings)
  db.py           async engine + session
  models.py       SQLAlchemy models
  schemas.py      Pydantic request/response schemas
  routers/        API routes
  services/       business logic
worker/           background email worker (consumes the Redis queue)
scripts/          seed data
migrations/       Alembic migrations
loadtests/        k6 scripts
observability/    prometheus.yml, Grafana provisioning, dashboards
tests/            pytest
```

## Rules

1. **Config from environment only.** All configuration comes from environment
   variables, loaded via pydantic-settings in `app/config.py`. Every setting must
   also appear in `.env.example` with a safe placeholder value. No hard-coded
   hosts, ports, credentials, or URLs in code.
2. **Never commit real secrets.** `.env` is in `.gitignore`. Only `.env.example`
   (placeholders) is committed.
3. **Every new feature gets at least one pytest test** in `tests/`.
4. **Keep the API fast.** Request handlers must not do slow work. Anything slow —
   especially sending email — goes through the Redis queue and is handled by
   `worker/`. Handlers write to the DB, enqueue, and return.
5. **Explain what you built** in plain language at the end of each task: what
   changed, why, and how to try it.

## Conventions

- Use async throughout (async SQLAlchemy sessions, redis-py asyncio).
- Keep routers thin; put logic in `app/services/`.
- Schema changes go through an Alembic migration in `migrations/`.
- Data is multi-tenant: every tenant-owned query must be scoped by tenant.
