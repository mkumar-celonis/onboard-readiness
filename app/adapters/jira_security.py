"""Jira security adapter.

Security tickets are identified by issue type (e.g. 'Vulnerability') in a
dedicated Jira project (e.g. Celonis 'CBE'). Severity comes from a custom
'Risk Rating' field with values Critical / High / Moderate / Low.

Config in projects.json → jira.security_filter:
  {
    "enabled": true,
    "security_project_keys": ["CBE"],
    "issue_types":     ["Vulnerability"],
    "severity_field":  "Risk Rating",            # or "customfield_13554"
    "severity_levels": ["Critical","High","Moderate","Low"],
    "required_fields": ["Vulnerability Detected Date and Time","Type of Vulnerability"],
    "aging_field":     "Vulnerability Detected Date and Time",
    "sla_critical_days": 14,
    "sla_high_days":     30,
    "security_slice_by": "component"
  }

Severity gating (in metrics.py):
  - any open Critical (or SLA-breached) → Critical hard-fail (RED)
  - any open High → Standard gap
"""
import datetime as dt, asyncio, statistics, urllib.parse
from ..config import JIRA_EMAIL, JIRA_TOKEN

DEFAULT_SECURITY = {
    "enabled": True,
    "security_project_keys": None,
    "issue_types":     ["Vulnerability"],
    "severity_field":  "Risk Rating",
    "severity_levels": ["Critical", "High", "Moderate", "Low"],
    "required_fields": [],
    "aging_field":     "created",
    "sla_critical_days": 14,
    "sla_high_days":     30,
    "security_slice_by": "component",
}

def _jql_field(name: str) -> str:
    """Wrap a JQL field for safe use — customfield IDs bare, everything else quoted."""
    if not name: return name
    if name.startswith("customfield_"): return name
    return f'"{name}"'

def _clause_field_not_empty(field: str) -> str:
    return f'{_jql_field(field)} is not EMPTY'

def _clause_issuetype(types: list[str]) -> str:
    if not types: return ""
    return "issuetype in (" + ", ".join(f'"{t}"' for t in types) + ")"

async def fetch_security(client, cfg: dict, slice_field: str | None = None,
                          slice_value: str | None = None, window_days: int | None = None):
    sec = cfg.get("security_filter") or DEFAULT_SECURITY
    if not sec.get("enabled", True):
        return {"security": {"enabled": False}}
    if not (JIRA_EMAIL and JIRA_TOKEN):
        raise RuntimeError("JIRA_EMAIL / JIRA_TOKEN not set")

    site = cfg["site"].rstrip("/")
    ver  = cfg.get("api_version", "3")
    auth = (JIRA_EMAIL, JIRA_TOKEN)

    # Which project(s) hold security tickets? (CBE, usually)
    sec_keys = sec.get("security_project_keys") or cfg.get("project_keys") or []
    if isinstance(sec_keys, list) and len(sec_keys) > 1:
        proj_clause = "project in (" + ", ".join(sec_keys) + ")"
    elif isinstance(sec_keys, list) and sec_keys:
        proj_clause = f'project = {sec_keys[0]}'
    else:
        proj_clause = f'project = {cfg["project_keys"][0]}'

    # Build the base filter
    clauses = []
    itc = _clause_issuetype(sec.get("issue_types") or [])
    if itc: clauses.append(itc)
    # required_fields — AND (mode=all, default, strict) or OR (mode=any, permissive)
    req_fields = sec.get("required_fields") or []
    req_mode   = (sec.get("required_fields_mode") or "all").lower()
    if req_fields:
        parts = [_clause_field_not_empty(f) for f in req_fields]
        if req_mode == "any" and len(parts) > 1:
            clauses.append("(" + " OR ".join(parts) + ")")
        else:
            clauses.extend(parts)
    filter_clause = " AND ".join(clauses)

    # Product-scope filter — OR across any of the configured identifier fields.
    # (Celonis uses "Product Assets" / "Service Assets" / "Squad Assets" — a ticket
    # might have any one of them tagged with the product name.)
    product_scope_clause = ""
    ps_field = sec.get("product_scope_field")
    ps_value = sec.get("product_scope_value")
    ps_fields = ps_field if isinstance(ps_field, list) else ([ps_field] if ps_field else [])
    if ps_fields and ps_value:
        or_clauses = [f'{_jql_field(f)} = "{ps_value}"' for f in ps_fields]
        product_scope_clause = " AND (" + " OR ".join(or_clauses) + ")"

    # Service-scope filter — applied only when a specific service is being queried
    sec_slice_by = sec.get("security_slice_by") or slice_field
    slice_clause = f' AND {_jql_field(sec_slice_by)} = "{slice_value}"' if (sec_slice_by and slice_value) else ""
    slice_clause = product_scope_clause + slice_clause

    # Time-window clause — filter by aging_field (default 'Vulnerability Detected Date and Time')
    # or fall back to 'created' if aging_field is 'created' or missing.
    aging_field = sec.get("aging_field") or "created"
    window_clause = ""
    if window_days:
        window_clause = f' AND {_jql_field(aging_field)} >= -{window_days}d'

    base = f'{proj_clause}{slice_clause}' + (f' AND {filter_clause}' if filter_clause else "") + window_clause

    severity_field  = sec.get("severity_field", "Risk Rating")
    levels          = sec.get("severity_levels") or ["Critical", "High", "Moderate", "Low"]

    async def count(jql: str) -> int:
        # Jira Cloud retired /rest/api/2/search (410 Gone) and /search/approximate-count
        # returns bucketed approximations that round small counts to 0.
        # Use v3 /search/jql with token pagination — fetch minimal fields, count issues.
        # Cap at 500 to stay bounded (a service with 500+ vulns is already blocking).
        total = 0
        token = None
        for _ in range(5):   # 5 pages × 100 = 500 max
            params = {"jql": jql, "maxResults": 100, "fields": "id"}
            if token: params["nextPageToken"] = token
            r = await client.get(f"{site}/rest/api/3/search/jql", params=params, auth=auth)
            r.raise_for_status()
            j = r.json()
            total += len(j.get("issues", []) or [])
            if j.get("isLast") or not j.get("nextPageToken"):
                return total
            token = j.get("nextPageToken")
        return total

    # ---- Parallel: total + open + severity counts + sample of open vulns for aging ----
    open_by_severity_tasks = [
        count(f'{base} AND statusCategory != Done AND {_jql_field(severity_field)} = "{lv}"')
        for lv in levels
    ]
    search_path = f"{site}/rest/api/{ver}/search" + ("/jql" if ver == "3" else "")
    aging_field = sec.get("aging_field") or "created"
    sample_fields = f'created,priority,resolutiondate,summary,{severity_field},{aging_field}'.replace(",,",",")
    sample_req = client.get(search_path, auth=auth, params={
        "jql": f'{base} AND statusCategory != Done ORDER BY created ASC',
        "maxResults": 100, "fields": sample_fields,
    })

    total, openc, *by_sev_and_sample = await asyncio.gather(
        count(base),
        count(f'{base} AND statusCategory != Done'),
        *open_by_severity_tasks,
        sample_req,
    )
    by_severity = dict(zip(levels, by_sev_and_sample[:len(levels)]))
    sample_r = by_sev_and_sample[len(levels)]
    sample_r.raise_for_status()
    issues = sample_r.json().get("issues", [])

    # Compute aging (uses the configured aging_field if present, else created)
    now = dt.datetime.utcnow()
    ages_critical, ages_high = [], []
    oldest_key = oldest_summary = None
    oldest_days = 0
    for it in issues:
        f = it.get("fields", {}) or {}
        # Extract severity value
        raw = f.get(severity_field) if severity_field.startswith("customfield_") else f.get(severity_field.lower().replace(" ", ""))
        # Fallback: try the display name (Risk Rating is often stored under customfield_XXXX)
        if raw is None: raw = f.get("Risk Rating") or f.get("customfield_13554") or f.get("priority")
        sev_name = (raw.get("value") if isinstance(raw, dict) else raw) or ""
        sev_name = str(sev_name)

        # Aging source
        date_str = None
        if aging_field.startswith("customfield_"):
            date_str = f.get(aging_field)
        else:
            date_str = f.get(aging_field) or f.get("created")
        if not date_str: continue
        try:
            c = dt.datetime.fromisoformat(str(date_str)[:19])
        except Exception:
            continue
        age = (now - c).days
        if sev_name.lower() == "critical": ages_critical.append(age)
        elif sev_name.lower() == "high":   ages_high.append(age)
        if age > oldest_days:
            oldest_days = age
            oldest_key  = it.get("key")
            oldest_summary = f.get("summary")

    sla_c = sec.get("sla_critical_days", 14)
    sla_h = sec.get("sla_high_days", 30)
    breach_critical = sum(1 for a in ages_critical if a > sla_c)
    breach_high     = sum(1 for a in ages_high     if a > sla_h)

    jira_ui_search_url = f"{site}/issues/?jql={urllib.parse.quote(base)}"
    return {
        "security": {
            "enabled": True,
            "security_project_keys": sec_keys or cfg.get("project_keys"),
            "issue_types": sec.get("issue_types") or [],
            "severity_field": severity_field,
            "window_days": window_days,
            "base_jql": base,
            "jira_ui_search_url": jira_ui_search_url,
            "total_vulnerabilities": total,
            "open_vulnerabilities":  openc,
            "open_by_severity": by_severity,   # keys: Critical / High / Moderate / Low
            "aging": {
                "oldest_open_days": oldest_days,
                "oldest_open_key":  oldest_key,
                "oldest_open_summary": oldest_summary,
                "median_age_critical_days": round(statistics.median(ages_critical), 1) if ages_critical else None,
                "median_age_high_days":     round(statistics.median(ages_high),     1) if ages_high     else None,
            },
            "sla": {"critical_days": sla_c, "high_days": sla_h,
                    "critical_breached": breach_critical, "high_breached": breach_high},
        }
    }
