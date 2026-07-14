"""FastAPI entrypoint. Run:  uvicorn app.main:app --reload"""
import asyncio, urllib.parse
import datetime as dt
import httpx
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException, Query, Body
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .registry import load, find
from .aggregator import build_report, _is_placeholder
from .adapters.jira import build_noise_clause, DEFAULT_NOISE
from .config import HTTP_TIMEOUT, JIRA_EMAIL, JIRA_TOKEN, DD_API_KEY, DD_APP_KEY

app = FastAPI(title="Project Onboarding Readiness — Live")

DEFAULT_JIRA_SITE = "https://celonis.atlassian.net"

def _window_days(window_months: Optional[int]) -> Optional[int]:
    """None/0 => no filter (all time). Otherwise months × 30."""
    if not window_months or window_months <= 0: return None
    return int(window_months) * 30

def _resolve_days(window_days: Optional[int], window_months: Optional[int]) -> Optional[int]:
    """Prefer an explicit window_days (supports sub-month windows like 7/14 days);
    fall back to months. 0 or None => all time."""
    if window_days is not None:
        return window_days if window_days > 0 else None
    return _window_days(window_months)

@app.get("/api/overview")
async def overview(window_months: Optional[int] = Query(6, ge=0, le=60),
                   window_days: Optional[int] = Query(None, ge=0, le=400),
                   include_hidden: bool = Query(False)):
    """Portfolio comes from projects.json. Entries with `enabled: false` are
    filtered out by default — pass ?include_hidden=true to see them."""
    reg = load()
    days = _resolve_days(window_days, window_months)
    projects = [p for p in reg["projects"]
                if include_hidden or p.get("enabled", True)]
    reports = await asyncio.gather(*(build_report(p, window_days=days) for p in projects))
    return JSONResponse(list(reports))

@app.get("/api/report/{key}")
async def report(key: str, window_months: Optional[int] = Query(6, ge=0, le=60),
                 window_days: Optional[int] = Query(None, ge=0, le=400)):
    p = find(key)
    if not p: raise HTTPException(404, f"Unknown project '{key}'")
    return JSONResponse(await build_report(p, window_days=_resolve_days(window_days, window_months)))


@app.post("/api/register")
async def register(body: dict = Body(...)):
    """Register (or replace) a project entry in projects.json using data you
    provide directly — no Jira discovery, no product_tag auto-detection, no
    Engineering Component resolution, no alias/slug guessing.

    Body shape:
    {
      "key":                 "TMT",                 # REQUIRED — Jira project key
      "name":                "Task Mining",         # optional, defaults to key
      "product_scope_value": "task-mining",         # REQUIRED — string OR list of strings
                                                    # (list for products like CE that span
                                                    # multiple Product Assets labels in CBE)
      "services": [                                  # REQUIRED — at least one entry
        {
          "name":           "TM Client",             # REQUIRED — display name
          "jira_scope":     "TM Client",             # optional, defaults to name
                                                     # Jira Component in the source project
          "security_scope": "task-mining-client"     # REQUIRED — CBE `Service internal` value
        }
      ],
      "enabled": true                                # optional, default true
    }

    Set enabled:false if you want the entry present but hidden from /api/overview.
    Pass ?replace=true to overwrite an existing entry; default is 409 if it exists.
    """
    from .registry import load_raw, save_raw

    key = (body.get("key") or "").strip()
    if not key: raise HTTPException(400, "'key' is required")

    psv = body.get("product_scope_value")
    if not psv: raise HTTPException(400, "'product_scope_value' is required (string or list)")

    services_in = body.get("services") or []
    if not isinstance(services_in, list) or not services_in:
        raise HTTPException(400, "'services' must be a non-empty list")

    services = []
    for i, s in enumerate(services_in):
        if not isinstance(s, dict):
            raise HTTPException(400, f"services[{i}] must be an object")
        sname = (s.get("name") or "").strip()
        sec   = (s.get("security_scope") or "").strip()
        if not sname:
            raise HTTPException(400, f"services[{i}].name is required")
        if not sec:
            raise HTTPException(400, f"services[{i}].security_scope is required")
        services.append({
            "name":            sname,
            "jira_scope":      (s.get("jira_scope") or sname).strip(),
            "security_scope":  sec,
        })

    entry = {
        "key":                 key,
        "name":                (body.get("name") or key).strip(),
        "enabled":             bool(body.get("enabled", True)),
        "product_scope_value": psv,
        "services":            services,
    }

    cfg = load_raw()
    entries = cfg.setdefault("projects", [])
    idx = next((i for i, e in enumerate(entries) if e.get("key") == key), -1)

    replace = str(body.get("replace", "false")).lower() in ("true", "1", "yes")
    if idx >= 0 and not replace:
        raise HTTPException(409,
            f"'{key}' is already registered. Pass \"replace\": true in the body to overwrite it, "
            f"or edit projects.json by hand.")

    if idx >= 0:
        entries[idx] = entry
        action = "replaced"
    else:
        entries.append(entry)
        action = "added"

    save_raw(cfg)
    return JSONResponse({
        "action":       action,
        "key":          key,
        "services":     len(services),
        "entry":        entry,
        "hint":         f"Written to projects.json. Reload the dashboard to see '{key}' on the portfolio.",
    })


@app.get("/api/jira/projects")
async def jira_projects(query: Optional[str] = Query(None),
                        max_projects: int = Query(1000, ge=1, le=5000)):
    """List Jira projects the token can see, paginating across ALL pages (up to max_projects).
    Optionally filters server-side via Jira's `query` (matches key or name)."""
    if not (JIRA_EMAIL and JIRA_TOKEN):
        raise HTTPException(400, "Jira credentials not set (JIRA_EMAIL/JIRA_TOKEN in .env)")
    reg = load()
    site = DEFAULT_JIRA_SITE
    for p in reg.get("projects", []):
        s = p.get("jira", {}).get("site")
        if s: site = s.rstrip("/"); break

    page_size = 100
    all_values: list[dict] = []
    total = 0
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        start_at = 0
        while len(all_values) < max_projects:
            params = {"maxResults": page_size, "startAt": start_at,
                      "orderBy": "-lastIssueUpdatedTime"}
            if query: params["query"] = query
            r = await client.get(f"{site}/rest/api/3/project/search",
                                 params=params, auth=(JIRA_EMAIL, JIRA_TOKEN))
            if r.status_code != 200:
                raise HTTPException(r.status_code, r.text[:400])
            payload = r.json()
            total = payload.get("total", 0)
            page = payload.get("values", [])
            all_values.extend(page)
            if payload.get("isLast", True) or not page or start_at + len(page) >= total:
                break
            start_at += len(page)

    registered = {p["jira"]["project_keys"][0]: p for p in reg.get("projects", []) if p.get("jira", {}).get("project_keys")}
    out = []
    for it in all_values[:max_projects]:
        key = it.get("key")
        out.append({"key": key, "name": it.get("name"),
                    "type": it.get("projectTypeKey"),
                    "url":  f"{site}/browse/{key}",
                    "registered": key in registered,
                    "registered_name": registered[key]["name"] if key in registered else None})
    return JSONResponse({"site": site, "total": total, "fetched": len(out), "projects": out})

@app.get("/api/debug/{key}")
async def debug(key: str, window_months: Optional[int] = Query(6, ge=0, le=60)):
    """Returns the exact JQL each service is using + a live count from Jira.
    Use this to prove per-service Jira slicing is being applied (or find why it isn't)."""
    p = find(key)
    if not p: raise HTTPException(404, f"Unknown project '{key}'")

    jira_cfg = p.get("jira", {})
    site     = jira_cfg.get("site", "").rstrip("/")
    ver      = jira_cfg.get("api_version", "3")
    slice_by = jira_cfg.get("slice_by", "component")
    proj_key = (jira_cfg.get("project_keys") or [""])[0]
    days     = _window_days(window_months)
    window_clause = f' AND created >= -{days}d' if days else ""
    noise_cfg = jira_cfg.get("noise_filter", DEFAULT_NOISE)
    noise_clause  = build_noise_clause(noise_cfg)

    out = []
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        for svc in p.get("services", []):
            scope_val = svc.get("jira_scope")
            slice_ok  = bool(scope_val) and not _is_placeholder(scope_val)
            slice_clause = f' AND {slice_by} = "{scope_val}"' if slice_ok else ""
            jql = f'project = {proj_key}{slice_clause}{window_clause}{noise_clause}'
            unscoped_jql = f'project = {proj_key}{window_clause}{noise_clause}'

            item = {"service": svc.get("name"), "jira_scope": scope_val,
                    "slice_by": slice_by, "slice_applied": slice_ok,
                    "window_months": window_months, "window_days": days,
                    "noise_filter_active": bool(noise_clause),
                    "noise_filter_config": noise_cfg,
                    "noise_clause": noise_clause,
                    "jql_used": jql,
                    "jira_ui_search_url": f"{site}/issues/?jql={urllib.parse.quote(jql)}",
                    "unscoped_jql": unscoped_jql,
                    "unscoped_search_url": f"{site}/issues/?jql={urllib.parse.quote(unscoped_jql)}"}

            if not (JIRA_EMAIL and JIRA_TOKEN):
                item["scoped_count"] = item["unscoped_count"] = None
                item["note"] = "Jira credentials not set."
            else:
                async def count(jql):
                    # v3 /search/jql with token pagination, cap 500.
                    total = 0
                    token = None
                    try:
                        for _ in range(5):
                            params = {"jql": jql, "maxResults": 100, "fields": "id"}
                            if token: params["nextPageToken"] = token
                            r = await client.get(f"{site}/rest/api/3/search/jql",
                                                  params=params, auth=(JIRA_EMAIL, JIRA_TOKEN))
                            r.raise_for_status()
                            j = r.json()
                            total += len(j.get("issues", []) or [])
                            if j.get("isLast") or not j.get("nextPageToken"):
                                return total
                            token = j.get("nextPageToken")
                        return total
                    except Exception as e:
                        return f"error: {type(e).__name__}: {e}"
                item["scoped_count"], item["unscoped_count"] = await asyncio.gather(
                    count(jql), count(unscoped_jql))
            out.append(item)
    return JSONResponse({"project": key, "services": out})

@app.get("/api/debug/security/{key}")
async def debug_security(key: str, test_issue: Optional[str] = Query(None)):
    """Peel the security JQL filter by filter. Runs stepwise counts against CBE so
    you can see EXACTLY which clause drops results to zero.

    If test_issue is provided (e.g. CBE-52169), each layer also checks whether that
    specific ticket passes the filter — the answer is 1 (passes) or 0 (excluded).
    """
    p = find(key)
    if not p: raise HTTPException(404, f"Unknown project '{key}'")
    if not (JIRA_EMAIL and JIRA_TOKEN):
        raise HTTPException(400, "Jira credentials not set")

    jira_cfg = p.get("jira", {})
    site     = jira_cfg.get("site", "").rstrip("/")
    ver      = jira_cfg.get("api_version", "3")
    sec      = jira_cfg.get("security_filter") or {}

    sec_project = (sec.get("security_project_keys") or ["CBE"])[0]
    ps_field = sec.get("product_scope_field")
    ps_value = sec.get("product_scope_value")
    ps_fields = ps_field if isinstance(ps_field, list) else ([ps_field] if ps_field else [])
    issue_types    = sec.get("issue_types") or []
    required_fields= sec.get("required_fields") or []
    req_mode       = (sec.get("required_fields_mode") or "all").lower()
    aging_field    = sec.get("aging_field") or "created"
    slice_by       = sec.get("security_slice_by") or "component"

    def q(f):
        if not f: return f
        if f.startswith("customfield_"): return f
        return f'"{f}"'

    layers = [("Baseline",                  f'project = {sec_project}')]
    # + product scope
    if ps_fields and ps_value:
        ors = " OR ".join(f'{q(f)} = "{ps_value}"' for f in ps_fields)
        layers.append(("+ product scope",   f'{layers[-1][1]} AND ({ors})'))
    # + issuetype
    if issue_types:
        it = "issuetype in (" + ", ".join(f'"{t}"' for t in issue_types) + ")"
        layers.append(("+ issuetype=Vulnerability", f'{layers[-1][1]} AND {it}'))
    # + required fields
    if required_fields:
        parts = [f'{q(f)} is not EMPTY' for f in required_fields]
        joined = " OR ".join(parts) if req_mode == "any" and len(parts) > 1 else " AND ".join(parts)
        joined = f'({joined})' if len(parts) > 1 else joined
        layers.append((f'+ required_fields ({req_mode})', f'{layers[-1][1]} AND {joined}'))
    # + time window (before service scope, so per-service tests share it)
    if aging_field:
        layers.append(("+ time window (last 180d)",
                       f'{layers[-1][1]} AND {q(aging_field)} >= -180d'))

    async def count(jql):
        # v3 /search/jql with token pagination, cap 500.
        try:
            total = 0
            token = None
            for _ in range(5):
                params = {"jql": jql, "maxResults": 100, "fields": "id"}
                if token: params["nextPageToken"] = token
                r = await client.get(f"{site}/rest/api/3/search/jql",
                                      params=params, auth=(JIRA_EMAIL, JIRA_TOKEN))
                r.raise_for_status()
                j = r.json()
                total += len(j.get("issues", []) or [])
                if j.get("isLast") or not j.get("nextPageToken"):
                    return total
                token = j.get("nextPageToken")
            return total
        except Exception as e:
            return f"error: {type(e).__name__}: {e}"

    async def passes_issue(jql, issue_key):
        """Does the given issue key pass this filter? Returns 1/0/error."""
        return await count(f'key = {issue_key} AND ({jql})')

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        out = []
        for label, jql in layers:
            item = {"layer": label, "jql": jql,
                    "count": await count(jql),
                    "search_url": f"{site}/issues/?jql={urllib.parse.quote(jql)}"}
            if test_issue:
                item[f"{test_issue}_passes"] = await passes_issue(jql, test_issue)
            out.append(item)

        # ---- Per-service scoping: which registered service (if any) does this ticket belong to? ----
        # Also grab the test issue's actual Service internal value so we know for sure.
        service_matches = []
        base_all_filters = layers[-1][1]  # includes product+issuetype+required+window
        for svc in p.get("services", []):
            scope_val = svc.get("security_scope")
            if not scope_val or _is_placeholder(scope_val):
                service_matches.append({"service": svc.get("name"), "security_scope": scope_val,
                                         "count": 0, "test_issue_matches": None,
                                         "note": "placeholder / no security_scope configured"})
                continue
            svc_jql = f'{base_all_filters} AND {q(slice_by)} = "{scope_val}"'
            svc_count = await count(svc_jql)
            item = {"service": svc.get("name"), "security_scope": scope_val,
                    "count": svc_count,
                    "search_url": f"{site}/issues/?jql={urllib.parse.quote(svc_jql)}"}
            if test_issue:
                item["test_issue_matches"] = await passes_issue(svc_jql, test_issue)
            service_matches.append(item)

        # Also fetch the actual Service internal value of the test ticket
        actual_value = None
        if test_issue:
            try:
                r = await client.get(f"{site}/rest/api/{ver}/issue/{test_issue}",
                                     params={"fields": slice_by, "expand": "names"},
                                     auth=(JIRA_EMAIL, JIRA_TOKEN))
                if r.status_code == 200:
                    payload = r.json()
                    # find customfield id for slice_by
                    names = payload.get("names", {}) or {}
                    cf_id = next((cid for cid, nm in names.items()
                                  if (nm or "").strip().lower() == slice_by.strip().lower()), None)
                    fields = payload.get("fields", {}) or {}
                    v = fields.get(cf_id) if cf_id else fields.get(slice_by)
                    if isinstance(v, dict): v = v.get("value") or v.get("name")
                    if isinstance(v, list) and v:
                        v0 = v[0]
                        v = v0.get("value") or v0.get("name") if isinstance(v0, dict) else str(v0)
                    actual_value = v
            except Exception as e:
                actual_value = f"error: {e}"

        return JSONResponse({"project": key, "test_issue": test_issue,
                              "test_issue_actual_service_internal": actual_value,
                              "layers": out, "per_service_matches": service_matches})

@app.get("/api/debug/customer/{key}")
async def debug_customer(key: str, test_issue: Optional[str] = Query(None)):
    """Peel the customer-issues JQL filter apart layer by layer, same idea as
    /api/debug/security — so you can see exactly which clause drops the count
    to zero for a given service (e.g. TM Client).

    If test_issue is provided (e.g. CBE-52169), each layer also checks whether
    that specific ticket passes the filter — the answer is 1 (passes) or 0 (excluded).
    """
    p = find(key)
    if not p: raise HTTPException(404, f"Unknown project '{key}'")
    if not (JIRA_EMAIL and JIRA_TOKEN):
        raise HTTPException(400, "Jira credentials not set")

    jira_cfg = p.get("jira", {})
    site     = jira_cfg.get("site", "").rstrip("/")
    ver      = jira_cfg.get("api_version", "3")
    cust     = jira_cfg.get("customer_filter") or {}

    cust_project = (cust.get("project_keys") or ["CBE"])[0]
    ps_field  = cust.get("product_scope_field")
    ps_value  = cust.get("product_scope_value")
    ps_fields = ps_field if isinstance(ps_field, list) else ([ps_field] if ps_field else [])
    inc_types = cust.get("issue_types") or []
    exc_types = cust.get("exclude_issue_types") or []
    slice_by  = cust.get("slice_by") or "component"

    def q(f):
        if not f: return f
        if f.startswith("customfield_"): return f
        return f'"{f}"'

    layers = [("Baseline",                 f'project = {cust_project}')]
    # + product scope
    if ps_fields and ps_value:
        ors = " OR ".join(f'{q(f)} = "{ps_value}"' for f in ps_fields)
        layers.append(("+ product scope",  f'{layers[-1][1]} AND ({ors})'))
    # + issuetype include/exclude
    if inc_types:
        it = "issuetype in (" + ", ".join(f'"{t}"' for t in inc_types) + ")"
        layers.append(("+ issuetype include", f'{layers[-1][1]} AND {it}'))
    if exc_types:
        it = "issuetype not in (" + ", ".join(f'"{t}"' for t in exc_types) + ")"
        layers.append(("+ issuetype exclude (not Vulnerability)", f'{layers[-1][1]} AND {it}'))
    # + time window (before service scope, so per-service tests share it)
    layers.append(("+ time window (last 180d)",
                   f'{layers[-1][1]} AND created >= -180d'))

    async def count(jql):
        try:
            total = 0
            token = None
            for _ in range(5):
                params = {"jql": jql, "maxResults": 100, "fields": "id"}
                if token: params["nextPageToken"] = token
                r = await client.get(f"{site}/rest/api/3/search/jql",
                                      params=params, auth=(JIRA_EMAIL, JIRA_TOKEN))
                r.raise_for_status()
                j = r.json()
                total += len(j.get("issues", []) or [])
                if j.get("isLast") or not j.get("nextPageToken"):
                    return total
                token = j.get("nextPageToken")
            return total
        except Exception as e:
            return f"error: {type(e).__name__}: {e}"

    async def passes_issue(jql, issue_key):
        return await count(f'key = {issue_key} AND ({jql})')

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        out = []
        for label, jql in layers:
            item = {"layer": label, "jql": jql,
                    "count": await count(jql),
                    "search_url": f"{site}/issues/?jql={urllib.parse.quote(jql)}"}
            if test_issue:
                item[f"{test_issue}_passes"] = await passes_issue(jql, test_issue)
            out.append(item)

        service_matches = []
        base_all_filters = layers[-1][1]
        for svc in p.get("services", []):
            scope_val = svc.get("security_scope")
            if not scope_val or _is_placeholder(scope_val):
                service_matches.append({"service": svc.get("name"), "security_scope": scope_val,
                                         "count": 0, "test_issue_matches": None,
                                         "note": "placeholder / no security_scope configured"})
                continue
            svc_jql = f'{base_all_filters} AND {q(slice_by)} = "{scope_val}"'
            svc_count = await count(svc_jql)
            item = {"service": svc.get("name"), "security_scope": scope_val,
                    "count": svc_count,
                    "search_url": f"{site}/issues/?jql={urllib.parse.quote(svc_jql)}"}
            if test_issue:
                item["test_issue_matches"] = await passes_issue(svc_jql, test_issue)
            service_matches.append(item)

        actual_value = None
        # Dump several *candidate* fields for the test ticket — not just slice_by —
        # so we can see at a glance which field (if any) actually carries per-service
        # routing info on customer-reported issues (they may not use "Service internal"
        # at all; e.g. they might use the standard "Component/s" field instead, or none).
        candidate_fields_seen = {}
        if test_issue:
            candidates = [slice_by, "Component/s", "Components", "Product Assets",
                          "Service Assets", "Squad Assets", "Customer Company Name",
                          "Customer Service level"]
            # de-dupe case-insensitively while preserving order
            seen_lower = set()
            candidates = [c for c in candidates if c and not (c.strip().lower() in seen_lower or seen_lower.add(c.strip().lower()))]
            try:
                r = await client.get(f"{site}/rest/api/{ver}/issue/{test_issue}",
                                     params={"fields": "*all", "expand": "names"},
                                     auth=(JIRA_EMAIL, JIRA_TOKEN))
                if r.status_code == 200:
                    payload = r.json()
                    names = payload.get("names", {}) or {}
                    fields = payload.get("fields", {}) or {}

                    def extract(label):
                        cf_id = next((cid for cid, nm in names.items()
                                      if (nm or "").strip().lower() == label.strip().lower()), None)
                        v = fields.get(cf_id) if cf_id else fields.get(label)
                        if isinstance(v, dict): v = v.get("value") or v.get("name")
                        if isinstance(v, list) and v:
                            out_list = []
                            for v0 in v:
                                out_list.append(v0.get("value") or v0.get("name") if isinstance(v0, dict) else str(v0))
                            v = out_list
                        return v

                    for label in candidates:
                        candidate_fields_seen[label] = extract(label)
                    actual_value = candidate_fields_seen.get(slice_by)
            except Exception as e:
                actual_value = f"error: {e}"
                candidate_fields_seen = {"error": str(e)}

        return JSONResponse({"project": key, "test_issue": test_issue,
                              "test_issue_actual_service_internal": actual_value,
                              "test_issue_candidate_fields": candidate_fields_seen,
                              "layers": out, "per_service_matches": service_matches})

@app.get("/api/debug/discover-components/{key}")
async def discover_components(key: str, sample: int = Query(50, ge=1, le=100),
                                target_field: Optional[str] = Query(None)):
    """Fetch a sample of real security tickets and return the distinct values of the
    Engineering Component field (or the field named in target_field).

    Uses Jira's expand=names to properly map display names to customfield IDs —
    this is what Jira Cloud actually returns in the response.
    """
    p = find(key)
    if not p: raise HTTPException(404, f"Unknown project '{key}'")
    if not (JIRA_EMAIL and JIRA_TOKEN):
        raise HTTPException(400, "Jira credentials not set")

    jira_cfg = p.get("jira", {})
    site = jira_cfg.get("site", "").rstrip("/")
    ver  = jira_cfg.get("api_version", "3")
    sec  = jira_cfg.get("security_filter") or {}
    sec_project = (sec.get("security_project_keys") or ["CBE"])[0]

    ps_field = sec.get("product_scope_field")
    ps_value = sec.get("product_scope_value")
    ps_fields = ps_field if isinstance(ps_field, list) else ([ps_field] if ps_field else [])
    issue_types = sec.get("issue_types") or []
    slice_by = target_field or sec.get("security_slice_by") or "Engineering Component"

    def q(f):
        if not f: return f
        if f.startswith("customfield_"): return f
        return f'"{f}"'

    clauses = [f'project = {sec_project}']
    if ps_fields and ps_value:
        clauses.append("(" + " OR ".join(f'{q(f)} = "{ps_value}"' for f in ps_fields) + ")")
    if issue_types:
        clauses.append("issuetype in (" + ", ".join(f'"{t}"' for t in issue_types) + ")")
    jql = " AND ".join(clauses) + " ORDER BY created DESC"

    search_path = f"{site}/rest/api/{ver}/search" + ("/jql" if ver == "3" else "")
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        # Request ALL fields + expand=names so we can find the customfield ID by display name
        r = await client.get(search_path, auth=(JIRA_EMAIL, JIRA_TOKEN), params={
            "jql": jql, "maxResults": sample,
            "fields": "*all", "expand": "names",
        })
    r.raise_for_status()
    payload = r.json()
    issues = payload.get("issues", [])
    names_map = payload.get("names", {})   # {customfield_XXX: "Display Name", ...}

    # Find the customfield ID whose display name matches slice_by (case-insensitive)
    target_id = None
    for cf_id, disp_name in names_map.items():
        if disp_name and disp_name.strip().lower() == slice_by.strip().lower():
            target_id = cf_id
            break

    # Also look up all customfields whose name contains keywords useful for debugging
    keywords = ["component", "product", "service", "squad", "asset", "risk", "vulnerab"]
    related_fields = [
        {"id": cf_id, "name": disp}
        for cf_id, disp in names_map.items()
        if disp and any(k in disp.lower() for k in keywords)
    ]

    from collections import Counter
    ec_counts = Counter()
    for it in issues:
        f = it.get("fields") or {}
        v = None
        if target_id and target_id in f:
            v = f[target_id]
        # Try a few name variants as fallback
        for k in [slice_by, "Engineering Component", "engineering component"]:
            if v is None and k in f: v = f[k]

        # Normalize the value to a string
        if v is None:
            ec_counts["<empty / not set>"] += 1
            continue
        if isinstance(v, dict):
            v = v.get("value") or v.get("name") or str(v)
        if isinstance(v, list):
            if not v: ec_counts["<empty / not set>"] += 1; continue
            for x in v:
                if isinstance(x, dict): ec_counts[x.get("value") or x.get("name") or str(x)] += 1
                else: ec_counts[str(x)] += 1
        else:
            ec_counts[str(v)] += 1

    # Also expose one raw sample ticket so user can inspect
    sample_raw = None
    if issues:
        s0 = issues[0]
        # Include only non-null fields to keep it readable
        raw_fields = {k: v for k, v in (s0.get("fields") or {}).items() if v not in (None, "", [], {})}
        sample_raw = {"key": s0.get("key"), "fields": raw_fields}

    return JSONResponse({
        "project": key,
        "target_field": slice_by,
        "target_customfield_id": target_id,
        "jql": jql,
        "sample_size": len(issues),
        "unique_engineering_components": [
            {"value": v, "count": n} for v, n in ec_counts.most_common()
        ],
        "related_fields_hint": related_fields,
        "sample_raw_ticket": sample_raw,
        "hint": "If the target_field couldn't be found, look at related_fields_hint or sample_raw_ticket to identify the correct field name. Pass ?target_field=<name> to re-query a different field.",
    })

@app.get("/api/debug/discover-eng-components/{key}")
async def discover_eng_components(key: str, sample: int = Query(100, ge=10, le=200)):
    """Discover the Engineering Component objectId → human name mapping for a project.
    Cross-references customfield_11293 (Assets CMDB ref) with customfield_11323
    (Service internal string) across all vulnerability tickets, then optionally
    resolves each objectId to its display name via the Jira Assets API."""
    p = find(key)
    if not p: raise HTTPException(404, f"Unknown project '{key}'")
    if not (JIRA_EMAIL and JIRA_TOKEN):
        raise HTTPException(400, "Jira credentials not set")

    jira_cfg = p.get("jira", {})
    site = jira_cfg.get("site", "").rstrip("/")
    ver  = jira_cfg.get("api_version", "3")
    sec  = jira_cfg.get("security_filter") or {}
    sec_project = (sec.get("security_project_keys") or ["CBE"])[0]

    ps_field = sec.get("product_scope_field")
    ps_value = sec.get("product_scope_value")
    ps_fields = ps_field if isinstance(ps_field, list) else ([ps_field] if ps_field else [])
    issue_types = sec.get("issue_types") or []

    def q(f):
        if not f: return f
        if f.startswith("customfield_"): return f
        return f'"{f}"'

    clauses = [f'project = {sec_project}']
    if ps_fields and ps_value:
        clauses.append("(" + " OR ".join(f'{q(f)} = "{ps_value}"' for f in ps_fields) + ")")
    if issue_types:
        clauses.append("issuetype in (" + ", ".join(f'"{t}"' for t in issue_types) + ")")
    jql = " AND ".join(clauses) + " ORDER BY created DESC"

    from collections import Counter, defaultdict
    ec_service_map = defaultdict(Counter)      # {objectId: Counter({service_internal_value: count})}
    ec_counts = Counter()                      # {objectId: total tickets}
    ec_workspaces = {}                         # {objectId: workspaceId}
    empty_ec_count = 0
    sample_ticket_per_ec = {}                  # {objectId: sample_ticket_key}

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        search_path = f"{site}/rest/api/{ver}/search" + ("/jql" if ver == "3" else "")
        r = await client.get(search_path, auth=(JIRA_EMAIL, JIRA_TOKEN), params={
            "jql": jql, "maxResults": sample,
            "fields": "customfield_11293,customfield_11323,summary,priority,created",
        })
        r.raise_for_status()
        issues = r.json().get("issues", [])

        for it in issues:
            f = it.get("fields", {}) or {}
            ec = f.get("customfield_11293")
            svc_int = f.get("customfield_11323") or ""
            if isinstance(svc_int, dict): svc_int = svc_int.get("value") or svc_int.get("name") or ""
            svc_int = str(svc_int).strip()

            if not ec:
                empty_ec_count += 1
                continue
            # ec is typically a list of dicts [{workspaceId, id, objectId}]
            items = ec if isinstance(ec, list) else [ec]
            for obj in items:
                if not isinstance(obj, dict): continue
                oid = obj.get("objectId")
                if not oid: continue
                ec_counts[str(oid)] += 1
                if svc_int:
                    ec_service_map[str(oid)][svc_int] += 1
                if obj.get("workspaceId") and str(oid) not in ec_workspaces:
                    ec_workspaces[str(oid)] = obj["workspaceId"]
                if str(oid) not in sample_ticket_per_ec:
                    sample_ticket_per_ec[str(oid)] = it.get("key")

        # Try to resolve object names via the Assets API (may 401/403 if token lacks scope)
        resolved_names = {}
        for oid, ws_id in ec_workspaces.items():
            for endpoint_tmpl in [
                f"{site}/gateway/api/jsm/assets/workspace/{ws_id}/v1/object/{oid}",
                f"https://api.atlassian.com/jsm/assets/workspace/{ws_id}/v1/object/{oid}",
                f"{site}/rest/insight/1.0/object/{oid}",
            ]:
                try:
                    rr = await client.get(endpoint_tmpl, auth=(JIRA_EMAIL, JIRA_TOKEN),
                                          headers={"Accept": "application/json"})
                    if rr.status_code == 200:
                        j = rr.json()
                        name = j.get("label") or j.get("name") or j.get("displayName")
                        if name:
                            resolved_names[oid] = name
                            break
                except Exception:
                    pass

    # Build the summary table
    table = []
    for oid, total in ec_counts.most_common():
        services_seen = list(ec_service_map[oid].items())
        top_service = services_seen[0][0] if services_seen else None
        table.append({
            "objectId": oid,
            "resolved_name": resolved_names.get(oid),
            "ticket_count": total,
            "top_service_internal": top_service,
            "service_internal_breakdown": dict(ec_service_map[oid]),
            "sample_ticket": sample_ticket_per_ec.get(oid),
        })

    return JSONResponse({
        "project": key,
        "jql": jql,
        "total_tickets_sampled": len(issues),
        "tickets_with_no_engineering_component": empty_ec_count,
        "assets_api_reachable": bool(resolved_names),
        "engineering_components": table,
        "hint": "Use 'top_service_internal' or 'resolved_name' to identify each object, then add its objectId (or the resolved name) to the matching service's engineering_component_ids / engineering_component_names in projects.json.",
    })

@app.get("/api/debug/unassigned/{key}")
async def debug_unassigned(key: str, window_months: Optional[int] = Query(None, ge=0, le=60),
                            include_all_time: bool = Query(True)):
    """Returns the actual list of unassigned vulnerabilities — those tagged at
    product level only (Service internal = 'task-mining') or with no Engineering
    Component / Service internal populated. Use for onboarding data-hygiene
    conversations with the Global team."""
    p = find(key)
    if not p: raise HTTPException(404, f"Unknown project '{key}'")
    if not (JIRA_EMAIL and JIRA_TOKEN):
        raise HTTPException(400, "Jira credentials not set")

    jira_cfg = p.get("jira", {})
    site = jira_cfg.get("site", "").rstrip("/")
    ver  = jira_cfg.get("api_version", "3")
    sec  = jira_cfg.get("security_filter") or {}
    sec_project = (sec.get("security_project_keys") or ["CBE"])[0]

    ps_field  = sec.get("product_scope_field")
    ps_value  = sec.get("product_scope_value")
    ps_fields = ps_field if isinstance(ps_field, list) else ([ps_field] if ps_field else [])
    issue_types = sec.get("issue_types") or []
    slice_by    = sec.get("security_slice_by") or "Service internal"
    aging_field = sec.get("aging_field") or "created"

    def q(f):
        if not f: return f
        if f.startswith("customfield_"): return f
        return f'"{f}"'

    clauses = [f'project = {sec_project}']
    if ps_fields and ps_value:
        clauses.append("(" + " OR ".join(f'{q(f)} = "{ps_value}"' for f in ps_fields) + ")")
    if issue_types:
        clauses.append("issuetype in (" + ", ".join(f'"{t}"' for t in issue_types) + ")")
    # "Unassigned" = Service internal empty OR product-level tag
    clauses.append(f'({q(slice_by)} is EMPTY OR {q(slice_by)} = "{ps_value}")')

    if window_months and not include_all_time:
        days = window_months * 30
        clauses.append(f'{q(aging_field)} >= -{days}d')

    jql = " AND ".join(clauses) + " ORDER BY created DESC"
    ui_url = f"{site}/issues/?jql={urllib.parse.quote(jql)}"

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        search_path = f"{site}/rest/api/{ver}/search" + ("/jql" if ver == "3" else "")
        r = await client.get(search_path, auth=(JIRA_EMAIL, JIRA_TOKEN), params={
            "jql": jql, "maxResults": 100,
            "fields": f'summary,priority,status,customfield_13554,{slice_by},{aging_field},created',
        })
        r.raise_for_status()
        payload = r.json()

    issues = []
    for it in payload.get("issues", []):
        f = it.get("fields", {}) or {}
        sev = f.get("customfield_13554")
        if isinstance(sev, dict): sev = sev.get("value") or sev.get("name")
        status = f.get("status") or {}
        pri = f.get("priority") or {}
        svc_int = f.get(slice_by)
        if isinstance(svc_int, dict): svc_int = svc_int.get("value") or svc_int.get("name")
        detected = f.get(aging_field) or f.get("created")
        issues.append({
            "key": it.get("key"),
            "url": f"{site}/browse/{it.get('key')}",
            "summary": f.get("summary"),
            "priority": pri.get("name"),
            "risk_rating": sev,
            "status": status.get("name"),
            "service_internal": svc_int or "<empty>",
            "detected": (detected or "")[:10],
        })

    return JSONResponse({
        "project": key,
        "jql": jql,
        "jira_ui_search_url": ui_url,
        "count": len(issues),
        "total_available": payload.get("total"),
        "issues": issues,
    })

import re, json

_JIRA_URL_KEY_RE = re.compile(r"/projects/([A-Za-z0-9_-]+)|/browse/([A-Za-z]+)-\d+")

def _parse_key(url_or_key: str) -> Optional[str]:
    """Given either a bare key ('TMT') or a Jira URL, return the project key uppercase."""
    if not url_or_key: return None
    s = url_or_key.strip()
    m = _JIRA_URL_KEY_RE.search(s)
    if m:
        return (m.group(1) or m.group(2)).upper()
    if re.fullmatch(r"[A-Za-z0-9_-]{1,15}", s):
        return s.upper()
    return None

# --- Default configuration blocks used to synthesize new project entries ---
_DEFAULT_JIRA_SITE = DEFAULT_JIRA_SITE  # celonis.atlassian.net

def _default_project_entry(key: str, name: str, product_tag: str) -> dict:
    """Return a MINIMAL project entry for projects.json — the human can then
    hand-edit (add per-service overrides, github/datadog wiring, etc.)."""
    return {
        "key": key,
        "name": name or key,
        "enabled": True,
        "product_scope_value": product_tag,
        "services": [],
    }

async def _detect_product_tag(client, site: str, project_key: str) -> Optional[str]:
    """Query the Jira project for sample tickets and return the most-common
    Product Assets / Service Assets / Squad Assets value seen."""
    from collections import Counter
    if not (JIRA_EMAIL and JIRA_TOKEN): return None
    r = await client.get(f"{site}/rest/api/3/search/jql", auth=(JIRA_EMAIL, JIRA_TOKEN), params={
        "jql": f'project = {project_key} ORDER BY created DESC',
        "maxResults": 50,
        "fields": "customfield_11062,customfield_11063,customfield_11064",
    })
    if r.status_code != 200: return None
    counts = Counter()
    for it in r.json().get("issues", []):
        f = it.get("fields", {}) or {}
        for cf in ("customfield_11062", "customfield_11063", "customfield_11064"):
            v = f.get(cf)
            if not v: continue
            items = v if isinstance(v, list) else [v]
            for x in items:
                if isinstance(x, dict):
                    name = x.get("name") or x.get("value") or x.get("objectId")
                    if name: counts[str(name)] += 1
                else:
                    counts[str(x)] += 1
    if not counts: return None
    return counts.most_common(1)[0][0]

async def _fetch_project_components(client, site: str, project_key: str) -> list[dict]:
    """Fetch the Components list defined on a Jira project. Each component
    typically represents a service/module within the product — the natural
    starting point for the services array in projects.json."""
    if not (JIRA_EMAIL and JIRA_TOKEN): return []
    r = await client.get(f"{site}/rest/api/3/project/{project_key}/components",
                         auth=(JIRA_EMAIL, JIRA_TOKEN))
    if r.status_code != 200: return []
    out = []
    for c in r.json() or []:
        out.append({
            "id":          c.get("id"),
            "name":        c.get("name"),
            "description": c.get("description") or "",
            "lead":        (c.get("lead") or {}).get("displayName"),
        })
    return out

async def _resolve_cmdb_label(client, site: str, workspace_id: str, object_id: str) -> Optional[str]:
    """Resolve a CMDB objectId to its display label via the Jira Assets API.
    Returns None on failure (token scope, permissions, etc.)."""
    for endpoint in [
        f"{site}/gateway/api/jsm/assets/workspace/{workspace_id}/v1/object/{object_id}",
        f"https://api.atlassian.com/jsm/assets/workspace/{workspace_id}/v1/object/{object_id}",
    ]:
        try:
            r = await client.get(endpoint, auth=(JIRA_EMAIL, JIRA_TOKEN),
                                  headers={"Accept": "application/json"})
            if r.status_code == 200:
                j = r.json()
                name = j.get("label") or j.get("name") or j.get("displayName")
                if name: return name
        except Exception:
            pass
    return None


async def _component_engineering_component(client, site: str, project_key: str,
                                           component_name: str) -> Optional[str]:
    """Read the project's tickets that carry this Jira Component, find the most
    common Engineering Component (customfield_11293 CMDB reference), resolve its
    label. That label is the *authoritative* security_scope for this service.
    Returns None if no tickets found or EC field unpopulated."""
    from collections import Counter
    if not (JIRA_EMAIL and JIRA_TOKEN): return None
    r = await client.get(f"{site}/rest/api/3/search/jql",
                          auth=(JIRA_EMAIL, JIRA_TOKEN),
                          params={
                              "jql": f'project = {project_key} AND component = "{component_name}" '
                                     f'ORDER BY updated DESC',
                              "maxResults": 30,
                              "fields": "customfield_11293",
                          })
    if r.status_code != 200: return None
    counter = Counter()
    workspaces = {}
    for it in r.json().get("issues", []):
        ec = (it.get("fields") or {}).get("customfield_11293")
        if not ec: continue
        items = ec if isinstance(ec, list) else [ec]
        for obj in items:
            if not isinstance(obj, dict): continue
            oid = obj.get("objectId")
            if not oid: continue
            counter[str(oid)] += 1
            if obj.get("workspaceId"):
                workspaces[str(oid)] = obj["workspaceId"]
    if not counter: return None
    top_oid, _ = counter.most_common(1)[0]
    ws_id = workspaces.get(top_oid)
    if not ws_id: return None
    return await _resolve_cmdb_label(client, site, ws_id, top_oid)


async def _discover_services_for_tag(client, site: str, product_tag: str) -> list[dict]:
    """Query CBE with the product tag; return a list of service dicts based on
    Service internal values seen. No service_tier — one common threshold applies."""
    from collections import Counter
    if not (JIRA_EMAIL and JIRA_TOKEN): return []
    def q(f): return f if f.startswith("customfield_") else f'"{f}"'
    jql = (f'project = CBE'
           f' AND ({q("Product Assets")} = "{product_tag}"'
           f'  OR {q("Service Assets")} = "{product_tag}"'
           f'  OR {q("Squad Assets")}   = "{product_tag}")'
           f' AND issuetype in ("Vulnerability")')
    r = await client.get(f"{site}/rest/api/3/search/jql", auth=(JIRA_EMAIL, JIRA_TOKEN), params={
        "jql": jql, "maxResults": 100,
        "fields": "customfield_11323",   # Service internal
    })
    if r.status_code != 200: return []
    seen = Counter()
    for it in r.json().get("issues", []):
        v = (it.get("fields") or {}).get("customfield_11323")
        if isinstance(v, dict): v = v.get("value") or v.get("name")
        if v and str(v).strip() and str(v).strip().lower() != product_tag.lower():
            seen[str(v).strip()] += 1
    services = []
    for value, _n in seen.most_common():
        # human name = title-case reformat of the kebab tag
        human = " ".join(w.capitalize() for w in re.split(r"[-_ ]+", value))
        services.append({
            "name": human,
            "jira_scope": "",
            "security_scope": value,
        })
    return services

@app.get("/api/jira/components/{key}")
async def jira_components(key: str):
    """List Jira components defined on a project. These are the natural
    services/modules within the product."""
    if not (JIRA_EMAIL and JIRA_TOKEN):
        raise HTTPException(400, "Jira credentials not set")
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        comps = await _fetch_project_components(client, _DEFAULT_JIRA_SITE, key)
    return JSONResponse({"project": key, "count": len(comps), "components": comps})

def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")

# Prefix-alias table: Celonis Jira Components use short abbreviations
# (`TM ...`, `CE ...`) while CBE `Service internal` uses the fully-spelled
# product tag (`task-mining-...`, `celonis-extractor-...`). Same service,
# different name. During matcher reconciliation we treat these as synonyms.
# Add new pairs here as more products are onboarded — no per-project config.
_PREFIX_ALIASES: list[tuple[str, str]] = [
    ("tm", "task-mining"),
    ("ce", "celonis-extractor"),
    ("bn", "business-networks"),
    ("dse", "data-science-engineering"),
]

def _canon_slugs(name: str) -> set[str]:
    """Return every slug form of `name` we should try to match against —
    the raw slug plus any prefix-alias substitutions. E.g. 'TM Chrome Extension'
    → {'tm-chrome-extension', 'task-mining-chrome-extension'}."""
    base = _slugify(name)
    out = {base}
    for short, long in _PREFIX_ALIASES:
        if base.startswith(f"{short}-"):
            out.add(f"{long}-{base[len(short)+1:]}")
        elif base.startswith(f"{long}-"):
            out.add(f"{short}-{base[len(long)+1:]}")
    return out

@app.get("/api/auto-onboard")
async def auto_onboard(url_or_key: str = Query(..., description="A Jira board URL or a bare project key"),
                       name: Optional[str] = Query(None),
                       persist: bool = Query(True),
                       force: bool = Query(False, description="If the project is already registered, re-run discovery and REPLACE its services array (leaves other settings untouched)."),
                       product_tag: Optional[str] = Query(None, description="Override the CBE product tag (Product Assets value). Skip auto-detection.")):
    """Generic onboarding: accepts a Jira URL or project key, auto-detects the
    CBE product tag, discovers services, appends to projects.json (if persist),
    and returns the resulting project entry. If the project is already registered
    without force=true, returns the existing entry unchanged. With force=true,
    re-runs component + CBE discovery and overwrites only the services array."""
    key = _parse_key(url_or_key)
    if not key: raise HTTPException(400, f"Could not parse a project key from: {url_or_key!r}")

    reg = load()
    existing_entry = None
    existing_idx = None
    for i, existing in enumerate(reg.get("projects", [])):
        if existing.get("key") == key:
            existing_entry = existing
            existing_idx = i
            break
    if existing_entry and not force:
        return JSONResponse({"already_registered": True, "key": key,
                              "entry": existing_entry,
                                  "hint": "Project already in projects.json. Set enabled:true if hidden, or pass ?force=true to re-run discovery."})

    site = _DEFAULT_JIRA_SITE
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        # Step 1: determine the CBE product tag.
        # Priority: (a) explicit product_tag param, (b) existing project's product_scope_value on force,
        # (c) auto-detect from source project's own tickets (works only if those tickets carry
        # the Assets fields — usually they don't, since Assets fields live on CBE tickets).
        detected_tag = None
        if product_tag:
            detected_tag = product_tag
        elif existing_entry and force:
            detected_tag = (((existing_entry.get("jira") or {}).get("security_filter") or {})
                            .get("product_scope_value"))
        if not detected_tag:
            detected_tag = await _detect_product_tag(client, site, key)
        if not detected_tag:
            raise HTTPException(400,
                f"Could not determine the CBE product tag for {key}. Product/Service/Squad Assets "
                f"fields are typically only populated on CBE tickets, not on the source project's own tickets — "
                f"so auto-detection from {key} tickets failed. Pass ?product_tag=<value> explicitly (e.g. "
                f"?product_tag=task-mining or ?product_tag=Celonis%20Extractor). To find the right value, "
                f"open any CBE ticket related to {key} and check its Product Assets / Service Assets / Squad Assets fields.")
        product_tag_final = detected_tag

        # Step 2a: fetch the project's real Jira Components — these define the
        #          services/modules within the product.
        components = await _fetch_project_components(client, site, key)
        # Step 2b: discover Service internal values from CBE vulns for this product
        cbe_services = await _discover_services_for_tag(client, site, product_tag_final)
        # Step 2c: for each Component, resolve its Engineering Component (CMDB) label.
        # This is the AUTHORITATIVE mapping — Celonis's ground truth for
        # Component → security_scope. Falls back to None if no tickets have EC set,
        # in which case alias/slug matching takes over.
        comp_names = [c.get("name") for c in components if c.get("name")]
        ec_labels = await asyncio.gather(*(
            _component_engineering_component(client, site, key, cn) for cn in comp_names
        ))
        ec_by_component = {cn: lbl for cn, lbl in zip(comp_names, ec_labels) if lbl}
    # Alias for use below (some references still use `product_tag` name)
    product_tag = product_tag_final

    # Step 3: merge Components (authoritative for service list + jira_scope)
    # with CBE Service internal values (authoritative for security_scope).
    # Match on a *set* of candidate slugs per side, so a Jira Component
    # named "TM Chrome Extension" (slug tm-chrome-extension) matches a CBE
    # Service internal named "task-mining-chrome-extension" via the alias table.
    cbe_lookup = { _slugify(s["security_scope"]): s["security_scope"] for s in cbe_services }
    claimed_cbe_slugs = set()
    comp_to_scope = {}   # component_name -> resolved security_scope

    # Pass 0 (NEW, AUTHORITATIVE): Engineering Component CMDB label wins if resolved.
    # Ground truth — Celonis's own linkage between a Jira Component and the security
    # scope its team files tickets under. Overrides any alias/slug guessing.
    for cn, ec_label in ec_by_component.items():
        comp_to_scope[cn] = ec_label
        claimed_cbe_slugs.add(_slugify(ec_label))

    # Pass 1: exact matches (via alias-expanded candidates). Order-independent
    # in effect since exact matches don't collide with each other.
    for comp in components:
        cn = comp.get("name") or ""
        if not cn or cn in comp_to_scope: continue
        for cand in _canon_slugs(cn):
            if cand in cbe_lookup and cand not in claimed_cbe_slugs:
                comp_to_scope[cn] = cbe_lookup[cand]
                claimed_cbe_slugs.add(cand)
                break

    # Pass 2: substring matching for anything left over. Skips already-claimed CBE scopes.
    for comp in components:
        cn = comp.get("name") or ""
        if not cn or cn in comp_to_scope: continue
        for cand in _canon_slugs(cn):
            hit = None
            for cbe_slug, cbe_val in cbe_lookup.items():
                if not cbe_slug or cbe_slug in claimed_cbe_slugs:
                    continue
                if cand in cbe_slug or cbe_slug in cand:
                    hit = (cbe_slug, cbe_val); break
            if hit:
                comp_to_scope[cn] = hit[1]
                claimed_cbe_slugs.add(hit[0])
                break

    # Pass 3: emit services in Jira Component order, filling any misses with slug fallback.
    services = []
    used_security_scopes = set()
    for comp in components:
        cn = comp.get("name") or ""
        if not cn: continue
        sec_scope = comp_to_scope.get(cn) or _slugify(cn)
        used_security_scopes.add(sec_scope)
        services.append({
            "name": cn,
            "jira_scope": cn,           # actual Jira Component name — authoritative
            "security_scope": sec_scope,
        })

    # CBE `Service internal` values that DIDN'T map to any Jira Component
    # are NOT services — they're a data-hygiene signal. Surface them as
    # metadata on the entry so the dashboard can flag them ("N tags in CBE
    # don't map to a TMT Component — either the tag is wrong or the component
    # is missing"), but keep them out of the services list. TMT's Jira
    # Components are the authoritative service registry.
    unmatched_cbe = [
        {"name": cbe["name"], "security_scope": cbe["security_scope"]}
        for cbe in cbe_services
        if cbe["security_scope"] not in used_security_scopes
    ]

    if persist:
        # Auto-onboard is now a *seed* tool — writes a new entry into projects.json
        # that the user can then hand-edit. Existing entries are only touched with
        # force=true.
        from .registry import load_raw, save_raw
        cfg = load_raw()
        entries = cfg.setdefault("projects", [])
        idx = next((i for i, e in enumerate(entries) if e.get("key") == key), -1)

        if idx >= 0 and force:
            entries[idx]["services"] = services
            entries[idx]["product_scope_value"] = product_tag
            entries[idx]["name"] = entries[idx].get("name") or name or key
            entries[idx]["unmatched_cbe_scopes"] = unmatched_cbe
            entry = entries[idx]
        elif idx >= 0:
            entry = entries[idx]
        else:
            entry = _default_project_entry(key, name or key, product_tag)
            entry["services"] = services
            entry["unmatched_cbe_scopes"] = unmatched_cbe
            entries.append(entry)
        save_raw(cfg)
    else:
        entry = existing_entry if (existing_entry and force) else _default_project_entry(key, name or key, product_tag)
        if not (existing_entry and force):
            entry["services"] = services
            entry["unmatched_cbe_scopes"] = unmatched_cbe

    return JSONResponse({"already_registered": bool(existing_entry) and force,
                          "refreshed": bool(existing_entry) and force,
                          "key": key,
                          "detected_product_tag": product_tag,
                          "jira_components_found": len(components),
                          "cbe_services_found": len(cbe_services),
                          "services_registered": len(services),
                          "unmatched_cbe_scopes": unmatched_cbe,
                          "entry": entry,
                          "hint": "Written to projects.json. Hand-edit to tune enabled or per-service thresholds. Reload the dashboard to see it on the portfolio."})

@app.get("/api/datadog/debug")
async def debug_datadog(key: Optional[str] = Query(None),
                        sample_tags: int = Query(40, ge=1, le=200)):
    """Verify the Datadog connection and help map monitors to services.

    - Confirms DD_API_KEY / DD_APP_KEY work against the configured site.
    - Lists the most common monitor tags in the org (so you can pick the right
      `service_tags` values for projects.json instead of guessing).
    - If ?key=TMT is passed, shows how many monitors each registered service's
      configured tags currently match — a quick way to spot wrong/empty tags.
    """
    if not (DD_API_KEY and DD_APP_KEY):
        raise HTTPException(400, "Datadog credentials not set (DD_API_KEY/DD_APP_KEY in .env)")

    # Resolve the Datadog site: project-level override if a key is given, else US1 default.
    site = "https://api.datadoghq.com"
    proj = None
    if key:
        proj = find(key)
        if not proj:
            raise HTTPException(404, f"Unknown project '{key}'")
        site = (proj.get("datadog") or {}).get("site", site)
    site = site.rstrip("/")
    H = {"DD-API-KEY": DD_API_KEY, "DD-APPLICATION-KEY": DD_APP_KEY}

    from collections import Counter
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        r = await client.get(f"{site}/api/v1/monitor", headers=H,
                             params={"page_size": 1000})
        if r.status_code != 200:
            raise HTTPException(r.status_code,
                f"Datadog returned {r.status_code} from {site}: {r.text[:300]} "
                f"(check the key/app-key pair and that the site matches your org).")
        monitors = r.json() or []

    tag_counts = Counter()
    for m in monitors:
        for t in (m.get("tags") or []):
            tag_counts[t] += 1

    out = {
        "site": site,
        "credentials_ok": True,
        "total_monitors": len(monitors),
        "top_tags": [{"tag": t, "monitors": n} for t, n in tag_counts.most_common(sample_tags)],
    }

    if proj:
        per_service = []
        for svc in proj.get("services", []):
            tags = (svc.get("datadog") or {}).get("service_tags") or []
            matched = None
            if tags:
                tag = tags[0]
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                    rr = await client.get(f"{site}/api/v1/monitor", headers=H,
                                          params={"monitor_tags": tag})
                    matched = len(rr.json() or []) if rr.status_code == 200 else f"error {rr.status_code}"
            per_service.append({"service": svc.get("name"),
                                "service_tags": tags,
                                "monitors_matched": matched})
        out["project"] = key
        out["per_service_matches"] = per_service
        out["hint"] = ("If monitors_matched is 0 for a service, its service_tags don't match any "
                       "monitor. Pick the correct value from top_tags and update projects.json.")

    return JSONResponse(out)

@app.get("/api/datadog/alerts/{key}")
async def datadog_alerts(key: str,
                         window_days: int = Query(30, ge=1, le=180),
                         limit: int = Query(25, ge=1, le=100)):
    """Recent Datadog monitor alerts for a project's services, with time-to-resolve
    (MTTR) and a manual-vs-auto resolution breakdown.

    Resolution type is derived from each monitor's `is_manual_resolve` option:
    monitors set to manual-resolve stay firing until a human clears them; all
    others auto-recover when the underlying metric returns to normal.
    """
    if not (DD_API_KEY and DD_APP_KEY):
        raise HTTPException(400, "Datadog credentials not set (DD_API_KEY/DD_APP_KEY in .env)")
    p = find(key)
    if not p:
        raise HTTPException(404, f"Unknown project '{key}'")

    site = (p.get("datadog") or {}).get("site", "https://api.datadoghq.com")

    from .adapters import datadog as dd_a
    services = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=45.0, write=10.0, pool=60.0),
                                 limits=httpx.Limits(max_connections=30, max_keepalive_connections=15)) as client:
        async def one(svc):
            tags = (svc.get("datadog") or {}).get("service_tags") or []
            if not tags:
                return {"service": svc.get("name"), "tag": None, "configured": False}
            try:
                hist = await dd_a.fetch_alert_history(client, site, tags[0],
                                                      window_days=window_days, limit=limit)
                return {"service": svc.get("name"), "tag": tags[0], "configured": True, **hist}
            except Exception as e:
                return {"service": svc.get("name"), "tag": tags[0], "configured": True,
                        "error": f"{type(e).__name__}: {e}"}
        services = await asyncio.gather(*(one(s) for s in p.get("services", [])))

    # Flatten across services: a most-recent-first alert feed, a priority-ranked
    # "top alerts" list, and the raw current/prior cycle lists used to compute the
    # VP rollups (all vs production, this window vs the prior window) with correct
    # medians (medians can't be summed across services, so we pool the cycles).
    all_alerts, top_all = [], []
    cyc_cur, cyc_prev = [], []
    for s in services:
        for a in s.get("alerts", []):
            all_alerts.append({**a, "service": s["service"]})
        for t in s.get("top", []):
            top_all.append({**t, "service": s["service"]})
        cyc_cur.extend(s.get("cycles", []))
        cyc_prev.extend(s.get("cycles_prev", []))
    all_alerts.sort(key=lambda a: a.get("triggered_at") or 0, reverse=True)
    top_all.sort(key=lambda t: (t.get("priority") or 99,
                                -(t.get("score") or 0),
                                -(t.get("last_triggered") or 0)))

    prod_cur = [c for c in cyc_cur if c.get("is_prod")]
    prod_prev = [c for c in cyc_prev if c.get("is_prod")]
    env_counts = {}
    for c in cyc_cur:
        env_counts[c.get("env") or "unknown"] = env_counts.get(c.get("env") or "unknown", 0) + 1

    vp = {
        "all": dd_a._summarize(cyc_cur),
        "all_prev": dd_a._summarize(cyc_prev),
        "prod": dd_a._summarize(prod_cur),
        "prod_prev": dd_a._summarize(prod_prev),
        "has_prod": bool(prod_cur) or any(c.get("is_prod") is False for c in cyc_cur),
        "environments": sorted(env_counts.items(), key=lambda kv: -kv[1]),
    }
    # Keep the legacy rollup keys the UI already reads, sourced from the all-env
    # current summary.
    roll = {k: vp["all"].get(k) for k in (
        "triggered", "resolved", "still_open", "auto_resolved", "manual_resolved",
        "mttr_hours_median", "mttr_hours_p90")}

    return JSONResponse({
        "project": key, "name": p.get("name"), "site": site,
        "window_days": window_days,
        "generated_at": dt.datetime.utcnow().isoformat() + "Z",
        "rollup": roll,
        "vp": vp,
        "recent": all_alerts[:limit],
        "top": top_all,
        "services": services,
    })

STATIC = Path(__file__).resolve().parents[1] / "static"
app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
