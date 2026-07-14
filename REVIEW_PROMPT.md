# Review Prompt — Celonis Onboarding Readiness Dashboard

Paste the section below into another AI (Claude, GPT, Gemini). It's self-contained. You can also point the reviewer at the codebase at `/Users/udaysoni/Desktop/onboarding-readiness/` if it has file access.

---

## Context

You are reviewing a dashboard I built for the **India Engineering team at Celonis**. Its purpose: score candidate projects for ownership-transfer decisions from the Global team to the India team. Data sources are **Jira Cloud** (main projects for tickets/components, plus a dedicated **CBE project** for security vulnerabilities and customer bugs), with optional GitHub and Datadog signals.

Please read the code and evaluate whether the design is sound, the implementation is correct, and the model matches Celonis's Jira reality.

## Architectural model (what the code should reflect)

1. **`projects.json`** holds **defaults only** — Jira site, CBE security-filter shape, customer-filter shape, thresholds. Org-level config, hand-authored, version-controlled. **No per-project entries.**
2. **`discovered.json`** holds an **auto-generated cache** of project entries — written by `/api/auto-onboard`, never hand-edited. Each entry contains only what's derived from Jira: `{key, name, product_scope_value, services[], unmatched_cbe_scopes[]}`.
3. **Services** for a project come from that project's **Jira Components** (authoritative). CBE is used only as a **filter source** — its `Product Assets` field scopes tickets to a product, its `Service internal` field scopes tickets to a service. CBE is not a service registry.
4. **Service-tier classification (Tier 1 / Tier 2 / On Prem / FrontEnd) has been removed**. One common threshold set applies to every service.
5. **`unmatched_cbe_scopes`** captures CBE `Service internal` values that don't correspond to any Jira Component in the source project — surfaced as a data-hygiene banner, not as fake services.

## Celonis Jira model (facts to hold in mind while reviewing)

- Each **product** has its own Jira project (TMT = Task Mining, CE = Cloud Extractors Suite, BN = Business Networks, DSE = ...).
- These projects have **Jira Components** that map to the services within the product (e.g. TMT has `TM Client`, `TM Gateway`, `TM AI Service`, ...).
- **All security tickets and customer bugs** across every product land in the **CBE** project — a shared bucket. Distinguishing which product/service they belong to happens via custom fields:
  - `Product Assets` (customfield_11062), `Service Assets`, `Squad Assets` — one of these holds the product tag (e.g. `task-mining`, `Celonis Extractor`).
  - `Service internal` (customfield_11323) — plain-text service identifier (e.g. `task-mining-client`).
- Security tickets are `issuetype = Vulnerability`; severity is in `Risk Rating` (customfield_13554: Critical/High/Moderate/Low).
- Customer tickets in CBE are non-Vulnerability; severity uses standard `priority` (Highest/High/Medium/Low).
- **Naming mismatch** you must handle: TMT's Jira Components use short prefixes (`TM ...`); CBE's `Service internal` uses full product names (`task-mining-...`). Same service, different name. An alias table (`_PREFIX_ALIASES`) collapses these during service matching: `tm ↔ task-mining`, `ce ↔ celonis-extractor`, `bn ↔ business-networks`, `dse ↔ data-science-engineering`.
- **Assets fields are only populated on CBE tickets, not on the source project's own tickets** — so `product_tag` can't be auto-detected from a project's own Jira data. It has to be passed on first onboard (`?product_tag=task-mining`) and is cached thereafter.

## Files to inspect

- `projects.json` — should contain only a top-level `defaults` block and an empty `projects: []` array.
- `discovered.json` — auto-cache; TMT should be present with 16 services and 7 unmatched CBE scopes.
- `app/registry.py` — reads defaults from projects.json, entries from discovered.json, merges via `_deep_merge`.
- `app/main.py` — FastAPI. Key endpoints: `GET /api/overview`, `GET /api/report/{key}`, `DELETE /api/project/{key}`, `GET /api/auto-onboard?url_or_key=X[&product_tag=Y][&force=true]`.
- `app/aggregator.py` — orchestrates parallel Jira/GitHub/Datadog fetches with `httpx.Limits(max_connections=10)`. Builds report with `project_view` (project-level totals) + `services[]` (per-service breakdown) + `unmatched_cbe_scopes[]`.
- `app/metrics.py` — one `DEFAULT_THRESHOLDS` dict. `evaluate_gates()` produces `critical_gaps[]` + `standard_gaps[]`. `compute_tier(gates)` maps to GREEN/AMBER/DEFERRED/RED. `rollup_tier()` = worst-service-wins.
- `app/adapters/jira.py`, `jira_security.py`, `jira_customer.py`, `github.py`, `datadog.py` — data sources.
- `static/index.html` — single-file dashboard: exec verdict, "How is this scored?" expander, headline cards (Security Debt, Customer Escalations, Service Readiness, Growth-Alignment), Top Risks (3 with more expander), service matrices (security + customer), unassigned/unmatched banners, insights section.

## Tier logic (metrics.py — verify)

```python
DEFAULT_THRESHOLDS = {
  "feature_pct_min": 80,
  "reopen_rate_max": 5,
  "merge_hours_max": 16,
  "reviewed_pct_min": 85,
  "muted_monitors_max": 3,
  "cycle_p90_max": 25,
}

def compute_tier(gates):
  if len(gates["critical_gaps"]) > 0:   return "RED"
  if len(gates["standard_gaps"]) == 0:  return "GREEN"
  if len(gates["standard_gaps"]) <= 6:  return "AMBER"
  return "DEFERRED"
```

Critical gates (any one → RED): no branch protection, any open Critical vuln, any Critical past SLA (14 days), any High past SLA (30 days).

## Verify these behaviors

1. **Fresh registration** — hitting `/api/auto-onboard?url_or_key=TMT&product_tag=task-mining` on an empty `discovered.json` should:
   - Detect product_tag `task-mining`
   - Fetch 16 Jira Components from TMT (via `/rest/api/3/project/TMT/components`)
   - Fetch CBE Service internal values scoped to `task-mining`
   - Alias-match each Component to a CBE tag (5 matches for TMT: TM Client ↔ task-mining-client, TM Gateway ↔ task-mining-gateway, TM Chrome Extension, TM AI Service, TM Uploader)
   - Write **16 services** to discovered.json, each with `{name, jira_scope, security_scope}` only
   - Write **7 unmatched CBE scopes** to `unmatched_cbe_scopes[]` (things like Task Mining Gateway that CBE knows about but Jira doesn't after alias matching — verify these should genuinely all be unmatched, or does the alias table have gaps)

2. **Re-registration** — hitting the same URL without `force=true` should return `already_registered: true` and not overwrite. With `force=true` it should regenerate services from Jira.

3. **Deletion** — `DELETE /api/project/TMT` should remove the entry from `discovered.json`.

4. **Security matrix** — for each service, count vulnerabilities from CBE where `project = CBE AND ("Product Assets" = "task-mining" OR ...) AND "Service internal" = <security_scope> AND issuetype = Vulnerability`. Sum by severity (Critical/High/Moderate/Low). Compute SLA breaches: any Critical > 14 days old = breach; any High > 30 days old = breach.

5. **Unassigned bucket** — `unassigned = project_total - sum(service_totals)`. These are tickets tagged at product level but with no/mismatched `Service internal`. Should surface as a row in the security matrix and a data-quality banner.

6. **Rollup** — project tier = worst service tier. One RED service → project is RED even if other 15 are GREEN.

7. **No service-tier residue** — the dashboard should render no "Tier 1", "Tier 2", "On Prem", "FrontEnd" text anywhere; no `.stier-*` CSS classes referenced; `service_tier` field absent from service entries.

## What I want you to check

Please give me an honest review across these axes:

1. **Model correctness** — is the "Jira Components = services, CBE = filter source" model actually reflected in the code paths, or does CBE data still bleed into service definitions anywhere?

2. **Alias matcher robustness** — the `_PREFIX_ALIASES` table only handles product-name prefix swaps. Look at the 7 `unmatched_cbe_scopes` in discovered.json — how many are legitimate orphans vs matcher gaps? Should any of `task-mining-gateway`, `task-mining-client`, `task-mining-chrome-extension`, `task-mining-ai`, `task-mining-uploader`, `tm-image-collector`, `cloud-task-mining` have merged into a Jira Component? Why did/didn't they?

3. **Discovery reliability** — auto-onboard depends on the user passing `product_tag=` on first registration for each project. Is that acceptable, or is there a way to derive it from CBE data automatically (e.g., cross-reference CBE tickets that mention the source project's key in title/body)?

4. **Threshold soundness** — is one common threshold set (5% reopen, 25d p90, 16h merge, 85% review, 3 muted, 80% feature share) reasonable for a mixed portfolio of customer-facing services and internal services? Where would you differentiate, and how would you do it without reintroducing manual `service_tier` labels?

5. **Rollup logic** — worst-service-wins for a project with 16 services is harsh. Is this defensible? Should the rollup weight critical services more, or downweight services with low ticket volume?

6. **Data-hygiene surfacing** — the unassigned row + unmatched-CBE-scopes banner are the only signals about data quality. Are there other silent failure modes worth surfacing (e.g., components with zero tickets in the time window)?

7. **API surface** — the REST endpoints `GET /overview`, `GET /report/{key}`, `GET /auto-onboard`, `DELETE /project/{key}` — is this a coherent set? What's missing that would be obvious to a user (e.g., listing what's in the cache, exporting a report)?

8. **Frontend UX** — the dashboard aims at a leadership audience (project verdict at a glance, transparent scoring, drill-in for detail). Does the code deliver on that? Look at `execHero`, `headlineCards`, `topRisks`, `scoringExplainerPanel` in `static/index.html`.

9. **Failure modes** — what happens if: Jira token expires? A component name has special characters? CBE has 10,000+ vulnerabilities for one product tag (pagination)? `product_scope_value` for two projects overlaps? The service list in discovered.json goes stale (e.g. new component added to Jira six months after registration)?

10. **Security** — credentials should live only in `.env`; are there any places tokens or emails could leak into logs/responses/persisted files? Any prompt-injection surface via Jira ticket titles/bodies that flow into JQL construction?

## What NOT to review

- Skill file (`register-onboarding-project`) — that's a Cowork skill definition, out of scope for a technical review.
- README, sample_response.json — user-facing docs, review separately if needed.
- Test coverage — there aren't tests yet; I know.

Please respond with:
- A prioritized list of bugs / correctness issues (highest impact first)
- A list of design concerns with rationale
- A list of "would improve but not blocking" suggestions
- Anything in the code that looks defensive/dead and should be cleaned up

Do not sugarcoat. If the model is wrong, say so.
