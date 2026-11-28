// Noisy neighbor load test.
//
// Two kinds of traffic at the same time:
//   noisy: acme sends batches of 100 at 5 requests/s  (= 500 emails/s)
//   quiet: initech, umbrella and hooli each send 2 single emails/s
//
// Question it answers: when one tenant floods the shared queue, how long do the
// other tenants' emails wait? Compare quiet tenants' delivery latency with and
// without the noisy scenario.
//
// Run (from the project folder, with the stack up and LOADTEST_MODE=true):
//   docker compose run --rm k6 run /loadtests/noisy_neighbor.js
// Baseline without the noisy tenant:
//   docker compose run --rm -e NOISY=off k6 run /loadtests/noisy_neighbor.js
//
// Options (environment variables):
//   DURATION=3m        how long both scenarios run
//   NOISY=off          skip the noisy scenario (baseline run)
//   DRAIN_TIMEOUT=600  max seconds to wait for the queue to empty before reporting
//
// API keys and customer ids come from loadtests/keys.json (written by scripts/seed.py).

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter } from 'k6/metrics';

const API = __ENV.API_URL;
if (!API) throw new Error('API_URL is not set (docker compose sets it for the k6 service)');
const ADMIN_TOKEN = __ENV.ADMIN_TOKEN;
const DURATION = __ENV.DURATION || '3m';
const NOISY_ON = (__ENV.NOISY || 'on') !== 'off';
const DRAIN_TIMEOUT = Number(__ENV.DRAIN_TIMEOUT || 600);

const NOISY_TENANT = 'acme';
const QUIET_TENANTS = ['initech', 'umbrella', 'hooli'];

const keys = JSON.parse(open('./keys.json'));
const tenants = Object.fromEntries(keys.map((k) => [k.tenant, k]));
for (const name of [NOISY_TENANT, ...QUIET_TENANTS]) {
  const t = tenants[name];
  if (!t || !t.customer_ids || t.customer_ids.length === 0) {
    throw new Error(`keys.json has no customer_ids for "${name}": run the seed again (python -m scripts.seed)`);
  }
}

// Responses by tenant and status, so 429s or 5xx show up clearly in the summary.
const accepted = new Counter('notifications_accepted');
const rejected = new Counter('notifications_rejected');

const scenarios = {};
if (NOISY_ON) {
  scenarios.noisy = {
    executor: 'constant-arrival-rate',
    exec: 'noisy',
    rate: 5,
    timeUnit: '1s',
    duration: DURATION,
    preAllocatedVUs: 20,
    maxVUs: 100,
    env: { TENANT: NOISY_TENANT },
  };
}
for (const name of QUIET_TENANTS) {
  scenarios[`quiet_${name}`] = {
    executor: 'constant-arrival-rate',
    exec: 'quiet',
    rate: 2,
    timeUnit: '1s',
    duration: DURATION,
    preAllocatedVUs: 5,
    maxVUs: 20,
    env: { TENANT: name },
  };
}

export const options = {
  scenarios,
  // teardown waits for the queue to drain, which can take a while under load.
  teardownTimeout: `${DRAIN_TIMEOUT + 60}s`,
  // Surfaces API latency per scenario in the end-of-test summary.
  thresholds: {
    'http_req_duration{scenario:noisy}': ['p(95)>=0'],
    'http_req_duration{tenant_class:quiet}': ['p(95)>=0'],
    'http_req_failed': ['rate<0.01'],
  },
  summaryTrendStats: ['avg', 'p(50)', 'p(95)', 'p(99)', 'max'],
};

function send(path, body, tenantName, tenantClass, emails) {
  const t = tenants[tenantName];
  const res = http.post(`${API}${path}`, JSON.stringify(body), {
    headers: { 'Content-Type': 'application/json', 'X-API-Key': t.api_key },
    tags: { tenant: tenantName, tenant_class: tenantClass },
  });
  const ok = check(res, { 'accepted (202)': (r) => r.status === 202 });
  const tags = { tenant: tenantName, status: String(res.status) };
  (ok ? accepted : rejected).add(emails, tags);
}

function pick(list, i) {
  return list[i % list.length];
}

export function noisy() {
  const t = tenants[__ENV.TENANT];
  const notifications = Array.from({ length: 100 }, (_, i) => ({
    customer_id: pick(t.customer_ids, i),
    subject: 'Weekly digest',
    body: 'Here is everything that happened this week.',
  }));
  send('/v1/notifications/batch', { notifications }, __ENV.TENANT, 'noisy', 100);
}

export function quiet() {
  const t = tenants[__ENV.TENANT];
  const body = {
    customer_id: pick(t.customer_ids, Math.floor(Math.random() * t.customer_ids.length)),
    subject: 'Your password reset code',
    body: 'Use code 123456 to reset your password.',
  };
  send('/v1/notifications', body, __ENV.TENANT, 'quiet', 1);
}

export function setup() {
  return { startedAt: Date.now() };
}

function admin(path) {
  const res = http.get(`${API}${path}`, {
    headers: { Authorization: `Bearer ${ADMIN_TOKEN}` },
    tags: { tenant_class: 'admin' },
  });
  if (res.status !== 200) throw new Error(`GET ${path} -> ${res.status}: ${res.body}`);
  return res.json();
}

// After the load stops: wait until every email is delivered, then print delivery
// latency per tenant from GET /admin/stats/delivery.
export function teardown(data) {
  if (!ADMIN_TOKEN) {
    console.warn('ADMIN_TOKEN not set: skipping the delivery report');
    return;
  }
  const drainStart = Date.now();
  let q = admin('/admin/queue/stats');
  console.log(`load finished. Queue right now: ${JSON.stringify(q)}`);
  while (q.queued + q.processing + q.delayed > 0 && (Date.now() - drainStart) / 1000 < DRAIN_TIMEOUT) {
    sleep(5);
    q = admin('/admin/queue/stats');
    console.log(`  draining... queued=${q.queued} processing=${q.processing} delayed=${q.delayed}`);
  }
  const drainSeconds = Math.round((Date.now() - drainStart) / 1000);

  // Exactly the notifications created during this run (not earlier runs).
  const since = new Date(data.startedAt).toISOString();
  const stats = admin(`/admin/stats/delivery?since=${encodeURIComponent(since)}`);

  const rows = stats.tenants.map((t) => [
    t.tenant, t.plan, t.created, t.sent, t.pending, t.failed,
    t.latency_ms.p50, t.latency_ms.p95, t.latency_ms.p99, t.latency_ms.max,
  ]);
  const header = ['tenant', 'plan', 'created', 'sent', 'pending', 'failed', 'p50 ms', 'p95 ms', 'p99 ms', 'max ms'];
  const widths = header.map((h, i) => Math.max(h.length, ...rows.map((r) => String(r[i] ?? '-').length)));
  const line = (r) => r.map((v, i) => String(v ?? '-').padStart(widths[i])).join('  ');

  console.log(
    [
      '',
      `===== delivery latency (sent_at - created_at) for this run (since ${since}), noisy=${NOISY_ON ? 'on' : 'off'} =====`,
      line(header),
      ...rows.map(line),
      `queue drained in ${drainSeconds}s after the load stopped; dead-letter: ${q.dead_letter}; workers: ${q.workers}`,
      '',
    ].join('\n'),
  );
}
