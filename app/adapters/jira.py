"""Jira adapter with time-window filter + configurable noise filter.

Noise filter is a project-level block that gets ANDed to every JQL:
  jira.noise_filter = {
    "enabled": true,
    "exclude_resolutions": ["Duplicate", "Won't Do", "Cannot Reproduce", "Invalid"],
    "exclude_labels":      ["test", "dummy"],
    "exclude_reporters":   ["bot@example.com"]
  }
Resolutions clause allows empty resolution (unresolved tickets stay in scope).
"""
import asyncio, datetime as dt, statistics
from ..config import JIRA_EMAIL, JIRA_TOKEN

# Sensible defaults if projects.json doesn't override.
DEFAULT_NOISE = {
    "enabled": True,
    "exclude_resolutions": ["Duplicate", "Won't Do", "Cannot Reproduce", "Invalid"],
    "exclude_labels":      [],
    "exclude_reporters":   [],
}

def build_noise_clause(nf: dict | None) -> str:
    """Returns a JQL fragment starting with ' AND ...' (or '' if disabled)."""
    if nf is None: nf = DEFAULT_NOISE
    if not nf.get("enabled", True): return ""
    clauses = []

    resolutions = nf.get("exclude_resolutions") or []
    if resolutions:
        # Escape any double-quotes inside the resolution value (e.g. Won't Do stays fine)
        vals = ", ".join(f'"{r}"' for r in resolutions)
        clauses.append(f"(resolution IS EMPTY OR resolution NOT IN ({vals}))")

    for lbl in (nf.get("exclude_labels") or []):
        clauses.append(f'labels != "{lbl}"')

    for reporter in (nf.get("exclude_reporters") or []):
        clauses.append(f'reporter != "{reporter}"')

    return (" AND " + " AND ".join(clauses)) if clauses else ""


async def fetch(client, cfg: dict, slice_field: str | None = None, slice_value: str | None = None,
                window_days: int | None = None):
    if not (JIRA_EMAIL and JIRA_TOKEN):
        raise RuntimeError("JIRA_EMAIL / JIRA_TOKEN not set")

    site  = cfg["site"].rstrip("/")
    ver   = cfg.get("api_version", "3")
    auth  = (JIRA_EMAIL, JIRA_TOKEN)
    key   = cfg["project_keys"][0]
    done  = ", ".join(f'"{s}"' for s in cfg["done_statuses"])
    tmap  = cfg["type_map"]
    noise_clause = build_noise_clause(cfg.get("noise_filter"))

    slice_clause    = f' AND {slice_field} = "{slice_value}"' if (slice_field and slice_value) else ""
    created_clause  = f' AND created >= -{window_days}d'  if window_days else ""
    resolved_clause = f' AND resolved >= -{window_days}d' if window_days else ""
    throughput_days = window_days if window_days else 28

    def scope_created(extra: str) -> str:
        base = f'project = {key}{slice_clause}{created_clause}{noise_clause}'
        return f'{base} AND ({extra})' if extra else base

    def scope_resolved(extra: str) -> str:
        base = f'project = {key}{slice_clause}{resolved_clause}{noise_clause}'
        return f'{base} AND ({extra})' if extra else base

    async def count(jql: str) -> int:
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

    def types_jql(names): return " OR ".join(f'issuetype = "{n}"' for n in names)

    search_path = f"{site}/rest/api/{ver}/search" + ("/jql" if ver == "3" else "")
    sample_req = client.get(search_path, auth=auth,
        params={"jql": scope_resolved("statusCategory = Done") + " ORDER BY resolved DESC",
                "maxResults": 50, "fields": "created,resolutiondate"})

    (total, openc, bug, feat, task, reopen, resolvedN,
     sev_c, sev_h, sev_m, sev_l, sample_r) = await asyncio.gather(
        count(scope_created("")),
        count(scope_created("statusCategory != Done")),
        count(scope_created(types_jql(tmap["bug"]))),
        count(scope_created(types_jql(tmap["feature"]))),
        count(scope_created(types_jql(tmap["task"]))),
        count(scope_resolved(f"status CHANGED FROM ({done})")),
        count(f'project = {key}{slice_clause} AND resolved >= -{throughput_days}d{noise_clause}'),
        count(scope_created('statusCategory != Done AND priority = "Highest"')),
        count(scope_created('statusCategory != Done AND priority = "High"')),
        count(scope_created('statusCategory != Done AND priority = "Medium"')),
        count(scope_created('statusCategory != Done AND priority = "Low"')),
        sample_req,
    )
    sample_r.raise_for_status()

    days = []
    for it in sample_r.json().get("issues", []):
        f = it.get("fields", {})
        if f.get("created") and f.get("resolutiondate"):
            c = dt.datetime.fromisoformat(f["created"][:19])
            s = dt.datetime.fromisoformat(f["resolutiondate"][:19])
            days.append((s - c).total_seconds() / 86400)
    median = round(statistics.median(days), 1) if days else 0.0
    p90    = round(sorted(days)[int(len(days) * 0.9)], 1) if len(days) > 1 else median

    closed    = total - openc
    typ_total = max(bug + feat + task, 1)
    weeks     = max(round(throughput_days / 7), 1)
    return {
        "tickets": {"total": total, "open": openc, "closed": closed,
                    "sev":  {"Critical": sev_c, "High": sev_h, "Medium": sev_m, "Low": sev_l},
                    "type": {"Bug": bug, "Feature": feat, "Task": task},
                    "bug_ratio_pct": round(bug / typ_total * 100)},
        "cycle":   {"median_days": median, "p90_days": p90},
        "reopen":  {"count": reopen, "rate_pct": round(reopen / max(closed, 1) * 100, 1)},
        "throughput_per_week": round(resolvedN / weeks),
        "roadmap": {"funded": True, "feature_pct": round(feat / typ_total * 100)},
    }
