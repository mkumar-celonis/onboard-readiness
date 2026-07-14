# Project Onboarding Readiness — Live Prototype

Stateless FastAPI service that pulls **live data** from Jira / GitHub / Datadog per project, computes onboarding-readiness metrics, and returns a per-project tier (Green / Amber / Maintenance / Red / Deferred). No database, no cache, **no mock data** — sources without credentials are honestly reported as "not configured".

## Structure

```
onboarding-readiness/
├── app/
│   ├── main.py            # FastAPI entry — /api/overview, /api/report/{key}
│   ├── config.py          # Loads .env, exposes credentials + runtime knobs
│   ├── registry.py        # Reads projects.json (the project registry)
│   ├── aggregator.py      # Orchestrates the live fan-out per project
│   ├── metrics.py         # Tier logic + gap evaluation
│   └── adapters/
│       ├── jira.py        # Jira Cloud v3 (or Server/DC v2) client
│       ├── github.py      # GitHub REST API client (multi-repo, deployment frequency, review coverage)
│       └── datadog.py     # Datadog v1 client
├── static/
│   └── index.html         # Dashboard (fetches /api/overview)
├── projects.json          # Project registry — one entry per project
├── GITHUB_INTEGRATION.md  # GitHub adapter detailed guide
├── requirements.txt
├── .env.example           # Copy to .env and fill in tokens
├── run.sh                 # One-shot: venv, install, run
└── README.md
```

## Run locally

```bash
cd onboarding-readiness
cp .env.example .env       # then edit .env with real credentials
./run.sh                   # creates .venv, installs, runs on :8000
```

Then open http://localhost:8000

Or manually:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

## API

Both endpoints are `GET`, take no body, and return JSON. There's no authentication in the prototype (add SSO before exposing beyond localhost). The dashboard at `/` is a static single-page app that calls `/api/overview` on load.

### `GET /api/overview`

Returns one report per project registered in `projects.json`.

**Request**

```
GET /api/overview HTTP/1.1
Host: localhost:8000
Accept: application/json
```

```bash
curl http://localhost:8000/api/overview
```

**Response** — `200 OK`, `Content-Type: application/json`. Body is a JSON array; each element has the same shape as `GET /api/report/{key}` (documented below).

```json
[
  { "key": "PAY",  "name": "Payments API", "tier": "GREEN", ... },
  { "key": "CHK",  "name": "Checkout Web", "tier": "AMBER", ... }
]
```

### `GET /api/report/{key}`

Returns the full readiness report for a single project. `{key}` matches the `key` field in `projects.json` (case-sensitive).

**Request**

```
GET /api/report/PAY HTTP/1.1
Host: localhost:8000
Accept: application/json
```

```bash
curl http://localhost:8000/api/report/PAY
```

**Response payload — full schema**

```json
{
  "key": "PAY",
  "name": "Payments API",
  "generated_at": "2026-07-02T10:14:03.221Z",
  "tier": "GREEN",

  "tickets": {
    "total": 842,
    "open": 96,
    "closed": 746,
    "sev":  { "Critical": 0, "High": 8, "Medium": 41, "Low": 47 },
    "type": { "Bug": 210, "Feature": 560, "Task": 72 },
    "bug_ratio_pct": 25
  },
  "cycle":  { "median_days": 2.8, "p90_days": 11 },
  "reopen": { "count": 19, "rate_pct": 2.5 },
  "throughput_per_week": 24,
  "roadmap": { "funded": true, "feature_pct": 83 },

  "github": {
    "open_prs": 6,
    "median_merge_hours": 5.2,
    "reviewed_pct": 99,
    "branch_protection": true
  },
  "datadog": {
    "monitors": 52,
    "muted": 1,
    "alerting": 0,
    "incidents": 0,
    "mttr_hours": 1.6
  },

  "gates": {
    "critical_gaps":  [],
    "strategic_fail": false,
    "standard_gaps":  []
  },

  "source_status": {
    "jira":    "ok",
    "github":  "ok",
    "datadog": "ok"
  }
}
```

**Field reference**

| Field | Type | Meaning |
|---|---|---|
| `key`, `name` | string | Identifiers from the registry |
| `generated_at` | ISO 8601 UTC string | When this report was computed |
| `tier` | `"GREEN"` / `"AMBER"` / `"MAINT"` / `"RED"` / `"DEFERRED"` | Auto-computed decision tier |
| `tickets.total` / `open` / `closed` | number \| null | Ticket counts (null if Jira not configured or errored) |
| `tickets.sev` | object | Open-ticket counts keyed by severity label |
| `tickets.type` | object | Total-ticket counts keyed by issue-type bucket (Bug/Feature/Task) — the buckets are defined per project in `projects.json` |
| `tickets.bug_ratio_pct` | number \| null | Bugs ÷ (Bugs+Features+Tasks), rounded |
| `cycle.median_days` / `p90_days` | number \| null | Time-to-close over a recent sample of resolved issues |
| `reopen.count` / `rate_pct` | number \| null | Issues transitioned back out of a Done status; rate is over closed issues |
| `throughput_per_week` | number \| null | Resolved issues ÷ 4 over the last 28 days |
| `roadmap.funded` / `feature_pct` | bool \| null / number \| null | Feature share of the type mix (see roadmap note below) |
| `github.*` | see above | GitHub signals; nulls when GitHub not configured or errored |
| `datadog.*` | see above | Datadog signals; nulls when Datadog not configured or errored |
| `gates.critical_gaps` | string[] | Human-readable list of failed Critical criteria (any → RED) |
| `gates.strategic_fail` | bool | True if Gate 1 (growth alignment) fails → MAINT |
| `gates.standard_gaps` | string[] | Standard-severity gaps; count drives Amber (1–6) / Deferred (7+) |
| `source_status.jira`/`github`/`datadog` | string | See values below |

**`source_status` values**

- `"ok"` — the adapter returned data successfully.
- `"not_configured"` — no credentials found in the environment; the section fields will be `null` and the gate is not scored for that source.
- `"error: <ErrorType>: <message>"` — the adapter tried but failed (bad token, wrong site URL, network, rate-limit). Other sources still render.

**Tier logic (evaluated top-to-bottom)**

```
if any critical_gap    → RED
elif strategic_fail    → MAINT
elif no standard_gaps  → GREEN
elif ≤ 6 standard_gaps → AMBER
else                   → DEFERRED
```

**Roadmap note.** `roadmap.feature_pct` in the current adapter is derived from Jira issue-type mix as a proxy for growth-vs-maintenance work; `roadmap.funded` is a placeholder returned as `true` when Jira is reachable. Replacing these with real signals (Advanced Roadmaps / Plans, funded-headcount confirmation) is a future enhancement.

### Errors

| Status | Body | When |
|---|---|---|
| `404 Not Found` | `{ "detail": "Unknown project 'XYZ'" }` | `key` not in `projects.json` |
| `500 Internal Server Error` | `{ "detail": "..." }` | Bug in the service itself. Source-level failures do **not** raise — they land in `source_status`. |

### Manual smoke test with `jq`

```bash
# Portfolio summary — one tier per project
curl -s http://localhost:8000/api/overview | jq -r '.[] | "\(.tier)\t\(.key)\t\(.name)"'

# One project — see which sources are live
curl -s http://localhost:8000/api/report/PAY | jq '.source_status'

# Full gate breakdown
curl -s http://localhost:8000/api/report/PAY | jq '.gates'
```

### Interactive docs

FastAPI exposes auto-generated OpenAPI docs while the server runs:

- Swagger UI:  http://localhost:8000/docs
- ReDoc:       http://localhost:8000/redoc
- Raw schema:  http://localhost:8000/openapi.json

### Examples & Postman

Working example payloads and an importable Postman collection live under `examples/`:

- `examples/sample_response.json`         — full response with all sources live
- `examples/sample_response_partial.json` — response when only Jira is configured (shows the `null` + `not_configured` semantics)
- `examples/postman_collection.json`      — Postman v2.1 collection; import into Postman, then edit the `baseUrl` and `projectKey` collection variables

## Add a project

Edit `projects.json` — add one entry with the project's Jira site + project key, GitHub repos, and Datadog service tag. No code change.

### GitHub Configuration

Each service can now have multiple GitHub repos:

```json
{
  "name": "Service Name",
  "jira_scope": "Component Name",
  "security_scope": "service-scope",
  "github": {
    "repos": [
      "owner/repo1",
      "owner/repo2"
    ]
  }
}
```

See [GITHUB_INTEGRATION.md](GITHUB_INTEGRATION.md) for detailed GitHub metrics, thresholds, and troubleshooting.

## What's live vs. not

Live from the source APIs when credentials are set:
- **Jira**: ticket counts, severity, cycle time (median/p90), reopens, bug/feature ratio
- **GitHub**: PR metrics (merge time, review coverage), branch protection, deployment frequency, commit rate (supports multi-repo aggregation)
- **Datadog**: monitor counts, alert states, incident data

Anything a source can't provide (or when credentials aren't set) is left as `null` and the section says "not configured" — nothing is faked. Two gates require deeper integration to auto-score and are left as future work: SCA/SAST (critical CVEs / EOL) and human-attested items (docs, runbooks).

## Notes

- Credentials come only from environment variables — never from `projects.json`.
- Adapters are fail-soft: if one source errors, its section shows the error and the rest of the report still renders.
- Jira Cloud uses `api_version: "3"` (default). Set to `"2"` for Server/Data Center.
