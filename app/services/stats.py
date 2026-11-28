"""Delivery statistics for admins (used to measure the noisy-neighbor problem)."""

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Delivery latency = sent_at - created_at (time from the API accepting the request
# to the worker handing the email to SMTP). percentile_cont ignores NULLs, so
# percentiles cover only sent notifications; counts cover everything created.
_DELIVERY_SQL = text(
    """
    SELECT
        t.id   AS tenant_id,
        t.name AS tenant,
        t.plan AS plan,
        count(*)                                     AS created,
        count(n.sent_at)                             AS sent,
        count(*) FILTER (WHERE n.status = 'queued')  AS pending,
        count(*) FILTER (WHERE n.status = 'failed')  AS failed,
        percentile_cont(ARRAY[0.5, 0.95, 0.99])
            WITHIN GROUP (ORDER BY extract(epoch FROM n.sent_at - n.created_at)) AS pcts,
        max(extract(epoch FROM n.sent_at - n.created_at)) AS max_s
    FROM notifications n
    JOIN tenants t ON t.id = n.tenant_id
    WHERE n.created_at >= COALESCE(CAST(:since AS timestamptz), now() - make_interval(mins => :minutes))
    GROUP BY t.id, t.name, t.plan
    ORDER BY created DESC, t.name
    """
)


def _ms(seconds) -> float | None:
    return None if seconds is None else round(float(seconds) * 1000, 1)


async def delivery_stats(session: AsyncSession, minutes: int, since: datetime | None = None) -> list[dict]:
    """Notifications created since `since`, or else in the last `minutes` minutes."""
    rows = (await session.execute(_DELIVERY_SQL, {"minutes": minutes, "since": since})).mappings().all()
    result = []
    for row in rows:
        p50, p95, p99 = row["pcts"] or (None, None, None)
        result.append(
            {
                "tenant_id": row["tenant_id"],
                "tenant": row["tenant"],
                "plan": row["plan"],
                "created": row["created"],
                "sent": row["sent"],
                "pending": row["pending"],
                "failed": row["failed"],
                "latency_ms": {"p50": _ms(p50), "p95": _ms(p95), "p99": _ms(p99), "max": _ms(row["max_s"])},
            }
        )
    return result
