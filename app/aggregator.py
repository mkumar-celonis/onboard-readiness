"""Parallelized aggregator with optional time-window filter."""
import asyncio, datetime as dt
import httpx

from .config  import HTTP_TIMEOUT, JIRA_EMAIL, JIRA_TOKEN, GITHUB_TOKEN, DD_API_KEY, DD_APP_KEY
from .metrics import compute_tier, evaluate_gates, resolve_thresholds, rollup_tier
from .adapters import jira as jira_a, github as gh_a, datadog as dd_a
from .adapters import jira_security as sec_a
from .adapters import jira_customer as cust_a
# ⚠️  DEMO MODE: Using mock GitHub adapter. Switch to real adapter when token is working.
from .adapters import github_mock as gh_mock
import urllib.parse

# Toggle this to switch between mock and real GitHub adapter
USE_MOCK_GITHUB = True  # Set to False to use real API


# Common abbreviation hints for Task Mining services (extendable per-project later).
# Ordered by specificity — check longer/more-specific patterns first.
_SERVICE_HINTS = [
    # (pattern in ticket summary, matching service_scope value)
    ("tm image collector",  "tm-image-collector"),
    ("tm-image-collector",  "tm-image-collector"),
    ("cloud-task-mining",   "cloud-task-mining"),
    ("cloud task mining",   "cloud-task-mining"),
    ("task-mining-uploader","task-mining-uploader"),
    ("task-mining-gateway", "task-mining-gateway"),
    ("task-mining-client",  "task-mining-client"),
    ("task-mining-chrome",  "task-mining-chrome-extension"),
    ("task-mining-ai",      "task-mining-ai"),
    ("task-mining-cloud-frontend", "task-mining-cloud-frontend"),
    ("task-mining-config-editor",  "task-mining-config-editor"),
    ("chrome extension",    "task-mining-chrome-extension"),
    ("windows client",      "task-mining-client"),
    ("windows service",     "task-mining-client"),
    ("windows",             "task-mining-client"),   # weak
    ("prompt injection",    "task-mining-ai"),
    ("rule discovery",      "task-mining-ai"),
    ("frontend",            "task-mining-cloud-frontend"),
    ("uploader",            "task-mining-uploader"),
    ("image collector",     "tm-image-collector"),
    ("gateway",             "task-mining-gateway"),
    ("tmc ",                "task-mining-client"),
    ("tmg ",                "task-mining-gateway"),
    ("tmu ",                "task-mining-uploader"),
]

def _suggest_service(summary: str, services: list[dict]) -> str | None:
    """Given a ticket summary and the registered services, return the name of the
    most likely service — or None if we can't infer confidently."""
    if not summary: return None
    s = summary.lower()
    # scope→name map from registered services
    scope_to_name = {(svc.get("security_scope") or "").lower(): svc.get("name")
                     for svc in services if svc.get("security_scope")}
    for pat, scope in _SERVICE_HINTS:
        if pat in s and scope in scope_to_name:
            return scope_to_name[scope]
    return None

def _blank_metrics():
    return {
        "tickets": {"total": None, "open": None, "closed": None, "sev": {}, "type": {}, "bug_ratio_pct": None},
        "cycle":   {"median_days": None, "p90_days": None},
        "reopen":  {"count": None, "rate_pct": None},
        "throughput_per_week": None,
        "roadmap": {"funded": None, "feature_pct": None},
        "github":  {
            "open_prs": None,
            "repos_monitored": None,
            "median_merge_hours": None,
            "reviewed_pct": None,
            "branch_protection": None,
            "avg_commits_per_week": None,
            "avg_releases_per_week": None,
            "deployment_frequency": None,
            "branch_protection_all": None,
        },
        "datadog": {"monitors": None, "muted": None, "alerting": None, "incidents": None, "mttr_hours": None},
    }

def _cfgd(src):
    if src == "jira":    return bool(JIRA_EMAIL and JIRA_TOKEN)
    if src == "github":  return bool(GITHUB_TOKEN)
    if src == "datadog": return bool(DD_API_KEY and DD_APP_KEY)
    return False

def _is_placeholder(value) -> bool:
    if not value: return True
    s = str(value).strip().lower()
    return s.startswith("todo") or s.startswith("<") or "todo-" in s

def _service_is_placeholder(service_cfg: dict) -> bool:
    if _is_placeholder(service_cfg.get("name")):        return True
    if _is_placeholder(service_cfg.get("jira_scope")):  return True
    repos = (service_cfg.get("github") or {}).get("repos") or []
    if repos and any(_is_placeholder(r) for r in repos): return True
    tags  = (service_cfg.get("datadog") or {}).get("service_tags") or []
    if tags  and any(_is_placeholder(t) for t in tags):  return True
    return False

async def _safe(coro, name):
    try:
        return await coro, "ok"
    except Exception as e:
        return None, f"error: {type(e).__name__}: {e}"

async def _build_service_report(client, project_cfg, service_cfg, window_days=None):
    m = _blank_metrics()
    status = {"jira": "not_configured", "github": "not_configured", "datadog": "not_configured",
              "security": "not_configured", "customer_issues": "not_configured"}

    jira_cfg  = project_cfg.get("jira", {})
    slice_by  = jira_cfg.get("slice_by", "component")
    slice_val = service_cfg.get("jira_scope")
    gh_cfg    = service_cfg.get("github")
    dd_cfg    = service_cfg.get("datadog")
    placeholder = _service_is_placeholder(service_cfg)

    tasks, keys = [], []
    if placeholder:
        if jira_cfg: status["jira"]    = "placeholder"
        if gh_cfg:   status["github"]  = "placeholder"
        if dd_cfg:   status["datadog"] = "placeholder"
        status["security"] = "placeholder"
    else:
        if jira_cfg and _cfgd("jira") and not _is_placeholder(slice_val):
            sf = None if slice_by == "none" else slice_by
            tasks.append(_safe(jira_a.fetch(client, jira_cfg, slice_field=sf, slice_value=slice_val, window_days=window_days), "jira"))
            keys.append("jira")
            # Per-service CBE fetches — used by the per-service summary row on the dashboard.
            # Each service shows: Jira total (above) + Security count + Customer count + Feature %.
            # No severity breakdown per service — just simple totals.
            sec_scope_val = service_cfg.get("security_scope") or slice_val
            tasks.append(_safe(sec_a.fetch_security(client, jira_cfg, slice_field=None, slice_value=sec_scope_val, window_days=window_days), "security"))
            keys.append("security")
            tasks.append(_safe(cust_a.fetch_customer(client, jira_cfg, slice_field=None, slice_value=sec_scope_val, window_days=window_days), "customer_issues"))
            keys.append("customer_issues")
        if gh_cfg:
            # Use mock adapter for demo, switch to real adapter when token works
            adapter = gh_mock if USE_MOCK_GITHUB else gh_a
            if not USE_MOCK_GITHUB and not _cfgd("github"):
                # Skip real GitHub if not configured
                pass
            else:
                tasks.append(_safe(adapter.fetch(client, gh_cfg), "github"))
                keys.append("github")
        if dd_cfg and _cfgd("datadog"):
            site = dd_cfg.get("site") or project_cfg.get("datadog", {}).get("site", "https://api.datadoghq.com")
            tasks.append(_safe(dd_a.fetch(client, {**dd_cfg, "site": site}), "datadog"))
            keys.append("datadog")

    if tasks:
        results = await asyncio.gather(*tasks)
        for k, (data, st) in zip(keys, results):
            status[k] = st
            if data: m.update(data)

    m["source_status"] = status
    th = resolve_thresholds(override=service_cfg.get("thresholds"))
    gates = evaluate_gates(m, th)
    return {
        "name": service_cfg["name"],
        "jira_scope": slice_val, "tier": compute_tier(gates),
        **m, "gates": gates,
    }

async def _fetch_project_jira(client, project_cfg, window_days=None):
    jira_cfg = project_cfg.get("jira", {})
    if not (jira_cfg and _cfgd("jira")):
        return None, "not_configured"
    try:
        return await jira_a.fetch(client, jira_cfg, slice_field=None, slice_value=None, window_days=window_days), "ok"
    except Exception as e:
        return None, f"error: {type(e).__name__}: {e}"

async def _fetch_project_security(client, project_cfg, window_days=None):
    jira_cfg = project_cfg.get("jira", {})
    if not (jira_cfg and _cfgd("jira")):
        return None, "not_configured"
    try:
        return await sec_a.fetch_security(client, jira_cfg, slice_field=None, slice_value=None, window_days=window_days), "ok"
    except Exception as e:
        return None, f"error: {type(e).__name__}: {e}"

async def _fetch_unassigned_tickets(client, project_cfg, services, window_days=None, limit=100):
    """Return the list of Task Mining vulns tagged only at product-level or with
    empty Service internal — for the 'Suggested service' enrichment table."""
    jira_cfg = project_cfg.get("jira", {})
    if not (jira_cfg and _cfgd("jira")): return []
    sec = jira_cfg.get("security_filter") or {}
    site = jira_cfg.get("site", "").rstrip("/")
    ver  = jira_cfg.get("api_version", "3")
    sec_project = (sec.get("security_project_keys") or ["CBE"])[0]

    ps_field  = sec.get("product_scope_field")
    ps_value  = sec.get("product_scope_value")
    ps_fields = ps_field if isinstance(ps_field, list) else ([ps_field] if ps_field else [])
    issue_types = sec.get("issue_types") or []
    slice_by    = sec.get("security_slice_by") or "Service internal"

    def q(f):
        if not f: return f
        if f.startswith("customfield_"): return f
        return f'"{f}"'

    clauses = [f'project = {sec_project}']
    if ps_fields and ps_value:
        clauses.append("(" + " OR ".join(f'{q(f)} = "{ps_value}"' for f in ps_fields) + ")")
    if issue_types:
        clauses.append("issuetype in (" + ", ".join(f'"{t}"' for t in issue_types) + ")")
    clauses.append(f'({q(slice_by)} is EMPTY OR {q(slice_by)} = "{ps_value}")')
    # Only open items for onboarding-blocking view
    clauses.append("statusCategory != Done")

    jql = " AND ".join(clauses) + " ORDER BY priority DESC, created DESC"
    search_path = f"{site}/rest/api/{ver}/search" + ("/jql" if ver == "3" else "")
    try:
        r = await client.get(search_path, auth=(JIRA_EMAIL, JIRA_TOKEN), params={
            "jql": jql, "maxResults": limit,
            "fields": f"summary,priority,status,customfield_13554,{slice_by},created",
        })
        r.raise_for_status()
    except Exception:
        return []
    out = []
    for it in r.json().get("issues", []):
        f = it.get("fields", {}) or {}
        pri = (f.get("priority") or {}).get("name")
        status = (f.get("status") or {}).get("name")
        sev = f.get("customfield_13554")
        if isinstance(sev, dict): sev = sev.get("value") or sev.get("name")
        svc_int = f.get(slice_by)
        if isinstance(svc_int, dict): svc_int = svc_int.get("value") or svc_int.get("name")
        summary = f.get("summary") or ""
        out.append({
            "key": it.get("key"),
            "url": f"{site}/browse/{it.get('key')}",
            "summary": summary,
            "priority": pri,
            "risk_rating": sev,
            "status": status,
            "service_internal": svc_int or None,
            "suggested_service": _suggest_service(summary, services),
        })
    return out

async def _fetch_unassigned_customer_tickets(client, project_cfg, services, window_days=None, limit=100):
    """Return customer-reported CBE issues that pass the product-scope/issuetype/window
    filters but carry no per-service tag (Service internal empty or product-level only).

    Confirmed via /api/debug/customer that customer-reported bugs are often tagged only
    with Customer Company Name / Customer Service level — no field identifying which
    internal service they belong to. Unlike the security side, there's no reliable field
    to fall back on, so this list exists mainly to surface the data-hygiene gap and offer
    a best-effort guess via the same title-based heuristic used for unassigned vulns."""
    jira_cfg = project_cfg.get("jira", {})
    if not (jira_cfg and _cfgd("jira")): return []
    cust = jira_cfg.get("customer_filter") or {}
    if not cust.get("enabled", True): return []
    site = jira_cfg.get("site", "").rstrip("/")
    ver  = jira_cfg.get("api_version", "3")
    cust_project = (cust.get("project_keys") or jira_cfg.get("project_keys") or ["CBE"])[0]

    ps_field  = cust.get("product_scope_field")
    ps_value  = cust.get("product_scope_value")
    ps_fields = ps_field if isinstance(ps_field, list) else ([ps_field] if ps_field else [])
    inc_types = cust.get("issue_types") or []
    exc_types = cust.get("exclude_issue_types") or []
    slice_by  = cust.get("slice_by") or "Service internal"

    def q(f):
        if not f: return f
        if f.startswith("customfield_"): return f
        return f'"{f}"'

    clauses = [f'project = {cust_project}']
    if ps_fields and ps_value:
        clauses.append("(" + " OR ".join(f'{q(f)} = "{ps_value}"' for f in ps_fields) + ")")
    if inc_types:
        clauses.append("issuetype in (" + ", ".join(f'"{t}"' for t in inc_types) + ")")
    if exc_types:
        clauses.append("issuetype not in (" + ", ".join(f'"{t}"' for t in exc_types) + ")")
    if window_days:
        clauses.append(f'created >= -{window_days}d')
    clauses.append(f'({q(slice_by)} is EMPTY OR {q(slice_by)} = "{ps_value}")')
    # Only open items for onboarding-blocking view
    clauses.append("statusCategory != Done")

    jql = " AND ".join(clauses) + " ORDER BY priority DESC, created DESC"
    search_path = f"{site}/rest/api/{ver}/search" + ("/jql" if ver == "3" else "")
    try:
        r = await client.get(search_path, auth=(JIRA_EMAIL, JIRA_TOKEN), params={
            "jql": jql, "maxResults": limit,
            "fields": f"summary,priority,status,{slice_by},created",
        })
        r.raise_for_status()
    except Exception:
        return []
    out = []
    for it in r.json().get("issues", []):
        f = it.get("fields", {}) or {}
        pri = (f.get("priority") or {}).get("name")
        status = (f.get("status") or {}).get("name")
        svc_int = f.get(slice_by)
        if isinstance(svc_int, dict): svc_int = svc_int.get("value") or svc_int.get("name")
        summary = f.get("summary") or ""
        out.append({
            "key": it.get("key"),
            "url": f"{site}/browse/{it.get('key')}",
            "summary": summary,
            "priority": pri,
            "status": status,
            "service_internal": svc_int or None,
            "suggested_service": _suggest_service(summary, services),
        })
    return out

async def _fetch_project_customer(client, project_cfg, window_days=None):
    jira_cfg = project_cfg.get("jira", {})
    if not (jira_cfg and _cfgd("jira")):
        return None, "not_configured"
    try:
        return await cust_a.fetch_customer(client, jira_cfg, slice_field=None, slice_value=None, window_days=window_days), "ok"
    except Exception as e:
        return None, f"error: {type(e).__name__}: {e}"

async def build_report(p: dict, window_days=None) -> dict:
    services = p.get("services", [])
    # Concurrency: with 16-service projects and 3 adapters per service, we launch
    # 50+ concurrent HTTP calls. Small pool + short pool timeout caused PoolTimeout
    # errors on the tail services (they'd wait past httpx's default 5s pool-wait).
    # Bumped max_connections to 25 and pool-wait to 60s. Jira Cloud tolerates ~30
    # concurrent connections from a single client; if throttling reappears, drop back to 15.
    limits = httpx.Limits(max_connections=25, max_keepalive_connections=15)
    timeout = httpx.Timeout(connect=10.0, read=45.0, write=10.0, pool=60.0)
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        results = await asyncio.gather(
            _fetch_project_jira(client, p, window_days=window_days),
            _fetch_project_security(client, p, window_days=window_days),
            _fetch_project_customer(client, p, window_days=window_days),
            *(_build_service_report(client, p, svc, window_days=window_days) for svc in services)
        )
    (proj_jira_data, proj_jira_status)         = results[0]
    (proj_sec_data,  proj_sec_status)          = results[1]
    (proj_cust_data, proj_cust_status)         = results[2]
    service_reports = results[3:]

    tiers = [s["tier"] for s in service_reports] or ["GREEN"]
    project_view = {"source_status": {"jira": proj_jira_status, "security": proj_sec_status,
                                       "customer_issues": proj_cust_status}}
    if proj_jira_data: project_view.update(proj_jira_data)
    if proj_sec_data:  project_view.update(proj_sec_data)
    if proj_cust_data: project_view.update(proj_cust_data)

    # ----- Compute "unassigned" buckets: vulns/issues tagged at product level
    # but not attributed to any registered service. Simple arithmetic since the
    # per-service queries are disjoint subsets of the project-level query. -----
    try:
        if proj_sec_status == "ok" and isinstance(project_view.get("security"), dict):
            proj_total = project_view["security"].get("total_vulnerabilities") or 0
            proj_open  = project_view["security"].get("open_vulnerabilities") or 0
            svc_total = sum(((s.get("security") or {}).get("total_vulnerabilities") or 0)
                            for s in service_reports if (s.get("source_status") or {}).get("security") == "ok")
            svc_open  = sum(((s.get("security") or {}).get("open_vulnerabilities") or 0)
                            for s in service_reports if (s.get("source_status") or {}).get("security") == "ok")
            unassigned_total = max(proj_total - svc_total, 0)
            unassigned_open  = max(proj_open  - svc_open,  0)
            pct_unassigned = round(unassigned_total / proj_total * 100) if proj_total else 0

            # Fetch the actual OPEN unassigned tickets + suggested-service enrichment
            tickets = []
            if unassigned_open > 0:
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as _client:
                    tickets = await _fetch_unassigned_tickets(_client, p, services, window_days=window_days)

            project_view["security"]["unassigned"] = {
                "total": unassigned_total, "open": unassigned_open,
                "pct_of_total": pct_unassigned,
                "tickets": tickets,
            }
    except Exception as e:
        # Never let an unassigned calc break the whole report
        pass

    try:
        if proj_cust_status == "ok" and isinstance(project_view.get("customer_issues"), dict):
            proj_total = project_view["customer_issues"].get("total") or 0
            proj_open  = project_view["customer_issues"].get("open") or 0
            svc_total = sum(((s.get("customer_issues") or {}).get("total") or 0)
                            for s in service_reports if (s.get("source_status") or {}).get("customer_issues") == "ok")
            svc_open  = sum(((s.get("customer_issues") or {}).get("open") or 0)
                            for s in service_reports if (s.get("source_status") or {}).get("customer_issues") == "ok")
            unassigned_total = max(proj_total - svc_total, 0)
            unassigned_open  = max(proj_open  - svc_open,  0)
            pct_unassigned = round(unassigned_total / proj_total * 100) if proj_total else 0

            # Fetch the actual OPEN unassigned tickets + suggested-service enrichment
            tickets = []
            if unassigned_open > 0:
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as _client:
                    tickets = await _fetch_unassigned_customer_tickets(_client, p, services, window_days=window_days)

            project_view["customer_issues"]["unassigned"] = {
                "total": unassigned_total, "open": unassigned_open,
                "pct_of_total": pct_unassigned,
                "tickets": tickets,
            }
    except Exception as e:
        pass

    # Security debt is a compliance obligation (own SLA clock), not an onboarding
    # gate. It transfers to the receiving team as-is and doesn't affect whether
    # the project is ready for transfer. Tier is now driven purely by per-service
    # operational signals (Jira delivery discipline, GitHub review hygiene, Datadog
    # monitor health, customer issues). The security count still shows in
    # "Overall Project Health" — it just doesn't block the tier.
    return {
        "key": p["key"], "name": p["name"],
        "generated_at": dt.datetime.utcnow().isoformat() + "Z",
        "window_days": window_days,
        "tier": rollup_tier(tiers),
        "rollup": {
            "critical_gaps_total": sum(len(s["gates"]["critical_gaps"]) for s in service_reports),
            "standard_gaps_total": sum(len(s["gates"]["standard_gaps"]) for s in service_reports),
            "any_strategic_fail":  any(s["gates"]["strategic_fail"] for s in service_reports),
            "service_count": len(service_reports),
        },
        # CBE `Service internal` values seen in security data that don't map to
        # any Jira Component in this project — surfaced as data hygiene, not
        # as services. Empty list means everything reconciles cleanly.
        "unmatched_cbe_scopes": p.get("unmatched_cbe_scopes", []),
        "project_view": project_view,
        "services": list(service_reports),
    }
