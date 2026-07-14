"""Customer-reported issues adapter.

Customer issues live in the same project as security (CBE at Celonis) but
have a different issue type — typically not 'Vulnerability'. Severity uses
P1/P2/P3/P4 (rather than Critical/High/Moderate/Low for vulns).

Config in projects.json → jira.customer_filter:
  {
    "enabled": true,
    "project_keys":   ["CBE"],
    "issue_types":    [],                     # empty = auto (everything except exclude)
    "exclude_issue_types": ["Vulnerability"], # exclude security types
    "severity_field": "priority",             # or "customfield_XXXXX"
    "severity_levels":["P1","P2","P3","P4"],
    "sla_p1_days": 3, "sla_p2_days": 14,
    "slice_by":  "component"
  }
"""
import datetime as dt, asyncio, statistics, urllib.parse
from ..config import JIRA_EMAIL, JIRA_TOKEN

DEFAULT_CUSTOMER = {
    "enabled": True,
    "project_keys": None,          # None = same as product project
    "issue_types":  [],            # empty = all
    "exclude_issue_types": ["Vulnerability"],
    "severity_field":  "priority",
    "severity_levels": ["P1","P2","P3","P4"],
    "sla_p1_days": 3,
    "sla_p2_days": 14,
    "slice_by":  "component",
}

def _q(f):
    if not f: return f
    if f.startswith("customfield_"): return f
    return f'"{f}"'

async def fetch_customer(client, cfg: dict, slice_field: str | None = None,
                          slice_value: str | None = None, window_days: int | None = None):
    cust = cfg.get("customer_filter") or DEFAULT_CUSTOMER
    if not cust.get("enabled", True):
        return {"customer_issues": {"enabled": False}}
    if not (JIRA_EMAIL and JIRA_TOKEN):
        raise RuntimeError("JIRA_EMAIL / JIRA_TOKEN not set")

    site = cfg["site"].rstrip("/")
    ver  = cfg.get("api_version", "3")
    auth = (JIRA_EMAIL, JIRA_TOKEN)

    keys = cust.get("project_keys") or cfg.get("project_keys") or []
    if isinstance(keys, list) and len(keys) > 1:
        proj_clause = "project in (" + ", ".join(keys) + ")"
    elif isinstance(keys, list) and keys:
        proj_clause = f'project = {keys[0]}'
    else:
        proj_clause = f'project = {cfg["project_keys"][0]}'

    # Filter — either includelist or excludelist
    filter_bits = []
    inc = cust.get("issue_types") or []
    exc = cust.get("exclude_issue_types") or []
    if inc:
        filter_bits.append("issuetype in (" + ", ".join(f'"{t}"' for t in inc) + ")")
    if exc:
        filter_bits.append("issuetype not in (" + ", ".join(f'"{t}"' for t in exc) + ")")
    filter_clause = " AND ".join(filter_bits)

    # Product-scope filter — OR across any of the configured identifier fields
    product_scope_clause = ""
    ps_field = cust.get("product_scope_field")
    ps_value = cust.get("product_scope_value")
    ps_fields = ps_field if isinstance(ps_field, list) else ([ps_field] if ps_field else [])
    if ps_fields and ps_value:
        or_clauses = [f'{_q(f)} = "{ps_value}"' for f in ps_fields]
        product_scope_clause = " AND (" + " OR ".join(or_clauses) + ")"

    slice_by = cust.get("slice_by") or slice_field
    slice_clause = f' AND {_q(slice_by)} = "{slice_value}"' if (slice_by and slice_value) else ""
    slice_clause = product_scope_clause + slice_clause

    window_clause = f' AND created >= -{window_days}d' if window_days else ""
    base = f'{proj_clause}{slice_clause}' + (f' AND {filter_clause}' if filter_clause else "") + window_clause

    severity_field  = cust.get("severity_field", "priority")
    levels          = cust.get("severity_levels") or ["P1","P2","P3","P4"]

    async def count(jql):
        # v2 /search is 410 Gone; approximate-count rounds small counts down.
        # Use v3 /search/jql with token pagination, cap 500.
        total = 0
        token = None
        for _ in range(5):
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

    open_by_severity_tasks = [
        count(f'{base} AND statusCategory != Done AND {_q(severity_field)} = "{lv}"')
        for lv in levels
    ]
    search_path = f"{site}/rest/api/{ver}/search" + ("/jql" if ver == "3" else "")
    sample_req = client.get(search_path, auth=auth, params={
        "jql": f'{base} AND statusCategory != Done ORDER BY created ASC',
        "maxResults": 100, "fields": f"created,priority,resolutiondate,summary,{severity_field}",
    })

    total, openc, *rest = await asyncio.gather(
        count(base),
        count(f'{base} AND statusCategory != Done'),
        *open_by_severity_tasks,
        sample_req,
    )
    by_severity = dict(zip(levels, rest[:len(levels)]))
    sample_r = rest[len(levels)]
    sample_r.raise_for_status()
    issues = sample_r.json().get("issues", [])

    # Aging is measured against the top-two configured severities (e.g. Highest/High or P1/P2)
    top1 = (levels[0] if len(levels) > 0 else "").lower()
    top2 = (levels[1] if len(levels) > 1 else "").lower()
    now = dt.datetime.utcnow()
    ages_top1, ages_top2 = [], []
    oldest_key = oldest_summary = None
    oldest_days = 0
    for it in issues:
        f = it.get("fields", {}) or {}
        raw = f.get(severity_field) if severity_field.startswith("customfield_") else f.get(severity_field)
        if raw is None: raw = f.get("priority")
        sev = (raw.get("value") or raw.get("name") if isinstance(raw, dict) else raw) or ""
        sev = str(sev).lower()
        created = f.get("created")
        if not created: continue
        try: c = dt.datetime.fromisoformat(str(created)[:19])
        except Exception: continue
        age = (now - c).days
        if top1 and sev == top1:   ages_top1.append(age)
        elif top2 and sev == top2: ages_top2.append(age)
        if age > oldest_days:
            oldest_days = age; oldest_key = it.get("key"); oldest_summary = f.get("summary")

    sla_top1 = cust.get("sla_p1_days", 3)
    sla_top2 = cust.get("sla_p2_days", 14)
    breach_top1 = sum(1 for a in ages_top1 if a > sla_top1)
    breach_top2 = sum(1 for a in ages_top2 if a > sla_top2)

    jira_ui_search_url = f"{site}/issues/?jql={urllib.parse.quote(base)}"
    return {
        "customer_issues": {
            "enabled": True,
            "project_keys": keys or cfg.get("project_keys"),
            "severity_field": severity_field,
            "severity_levels": levels,
            "window_days": window_days,
            "base_jql": base,
            "jira_ui_search_url": jira_ui_search_url,
            "total": total,
            "open":  openc,
            "open_by_severity": by_severity,   # keys match configured levels (e.g. Highest/High/Medium/Low)
            "aging": {
                "oldest_open_days": oldest_days,
                "oldest_open_key":  oldest_key,
                "oldest_open_summary": oldest_summary,
                "top1_label": levels[0] if levels else None,
                "top2_label": levels[1] if len(levels) > 1 else None,
                "median_age_top1_days": round(statistics.median(ages_top1), 1) if ages_top1 else None,
                "median_age_top2_days": round(statistics.median(ages_top2), 1) if ages_top2 else None,
            },
            "sla": {"top1_days": sla_top1, "top2_days": sla_top2,
                    "top1_breached": breach_top1, "top2_breached": breach_top2,
                    "top1_label": levels[0] if levels else None,
                    "top2_label": levels[1] if len(levels) > 1 else None},
        }
    }
