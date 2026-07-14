"""Live Datadog adapter."""
import asyncio
import re
import time
import urllib.parse
import statistics
from ..config import DD_API_KEY, DD_APP_KEY


def app_base(api_site: str) -> str:
    """Convert a Datadog *API* base (api.datadoghq.com) into the *app* UI base
    (app.datadoghq.com) so we can build clickable deep links. Handles all sites:
      api.datadoghq.com     -> app.datadoghq.com
      api.datadoghq.eu      -> app.datadoghq.eu
      api.us3.datadoghq.com -> us3.datadoghq.com  (region already in host)
    """
    host = (api_site or "").split("://", 1)[-1].strip("/")
    if host.startswith("api."):
        host = host[4:]
    if host.startswith("datadoghq."):
        host = "app." + host
    return "https://" + host


def _manage_url(app: str, tag: str, alerting_only: bool = False) -> str:
    """Deep link to the Datadog Manage Monitors page filtered to a service tag
    (optionally only those currently in Alert)."""
    q = f'tag:"{tag}"'
    if alerting_only:
        q = f'status:Alert {q}'
    return f'{app}/monitors/manage?q={urllib.parse.quote(q)}'


async def fetch(client, cfg):
    if not (DD_API_KEY and DD_APP_KEY):
        raise RuntimeError("DD_API_KEY / DD_APP_KEY not set")
    site = cfg.get("site", "https://api.datadoghq.com").rstrip("/")
    H = {"DD-API-KEY": DD_API_KEY, "DD-APPLICATION-KEY": DD_APP_KEY}

    tag = cfg["service_tags"][0]
    r = await client.get(f"{site}/api/v1/monitor",
                         params={"monitor_tags": tag}, headers=H)
    r.raise_for_status()
    mons = r.json()
    muted    = sum(1 for m in mons if (m.get("options") or {}).get("silenced"))
    alerting = sum(1 for m in mons if m.get("overall_state") == "Alert")
    app = app_base(site)
    return {"datadog": {"monitors": len(mons), "muted": muted, "alerting": alerting,
                        "incidents": 0, "mttr_hours": 0,
                        "service_tag": tag,
                        "monitors_url": _manage_url(app, tag),
                        "alerting_url": _manage_url(app, tag, alerting_only=True)}}


# ---------------------------------------------------------------------------
# Alert history — recent monitor alerts, time-to-resolve, and manual vs auto.
# ---------------------------------------------------------------------------

_PRIORITY_RE = re.compile(r"\[P(\d)\]")
_STATUS_TOKENS = ["Re-Triggered", "Triggered", "Recovered", "Renotify",
                  "No Data", "Warn"]


def _parse_priority(title, fallback=None):
    m = _PRIORITY_RE.search(title or "")
    return int(m.group(1)) if m else fallback


_HANDLE_RE = re.compile(r"@[\w][\w.\-]*(?:@[\w.\-]+)?")
# Handles that are template directives / noise rather than a person or team.
_HANDLE_SKIP = {"dependency"}


def _handles(message):
    """Notification @handles on a monitor (Slack channel, PagerDuty, on-call,
    team, email). These are the real 'who gets paged / owns this' targets."""
    out, seen = [], set()
    for h in _HANDLE_RE.findall(message or ""):
        name = h.lstrip("@")
        low = name.lower()
        if low in _HANDLE_SKIP or low in seen:
            continue
        seen.add(low)
        out.append(name)
    return out[:6]


def _owner_list(notify, creator):
    """Display owners: notification targets if any, else the monitor creator."""
    if notify:
        return notify[:4]
    c = creator or {}
    who = c.get("name") or c.get("email") or c.get("handle")
    return [who] if who else []


_ENV_RE = re.compile(r"env:([a-z0-9_.\-]+)", re.I)
_NONPROD = ("beta", "try", "dev", "develop", "development", "staging", "stage",
            "sandbox", "test", "qa", "integration", "canary", "local")
_PROD = ("prod", "production", "live")


def _env_of(group, title):
    """Best-effort environment for an alert: read env:<x> from the monitor group,
    else scan the title's bracket tokens (e.g. '[PRR | try | task-mining-ai]').
    Returns (env_label, is_prod) where is_prod may be None when unknown."""
    text = f"{group or ''} {title or ''}".lower()
    m = _ENV_RE.search(text)
    env = m.group(1) if m else None
    if not env:
        for tok in re.split(r"[|\[\]{}:,()\s]+", text):
            if tok in _NONPROD or tok in _PROD:
                env = tok
                break
    if not env:
        return None, None
    is_prod = any(env.startswith(p) for p in _PROD) or not any(env.startswith(n) for n in _NONPROD)
    return env, is_prod


def _summarize(cycles):
    """Aggregate a list of alert cycles into the VP-level numbers: exposure,
    responsiveness (MTTR overall + P1/P2), noise, and ownership coverage."""
    resolved = [c for c in cycles if c.get("status") == "resolved"]
    open_cy = [c for c in cycles if c.get("status") == "open"]
    durs = [c["duration_hours"] for c in resolved if c.get("duration_hours") is not None]
    p1p2_durs = [c["duration_hours"] for c in resolved
                 if c.get("duration_hours") is not None and (c.get("priority") or 9) <= 2]
    mons = {}
    for c in cycles:
        mid = c.get("monitor_id")
        mons[mid] = mons.get(mid, False) or bool(c.get("has_notify"))
    unowned = sum(1 for v in mons.values() if not v)
    fast_auto = sum(1 for c in resolved
                    if c.get("resolve_type") == "auto" and (c.get("duration_hours") or 0) <= 5 / 60.0)
    return {
        "triggered": len(cycles),
        "resolved": len(resolved),
        "still_open": len(open_cy),
        "open_p1p2": sum(1 for c in open_cy if (c.get("priority") or 9) <= 2),
        "auto_resolved": sum(1 for c in resolved if c.get("resolve_type") == "auto"),
        "manual_resolved": sum(1 for c in resolved if c.get("resolve_type") == "manual"),
        "fast_auto": fast_auto,
        "noise_pct": round(100.0 * fast_auto / len(resolved)) if resolved else None,
        "mttr_hours_median": round(statistics.median(durs), 2) if durs else None,
        "mttr_hours_p90": round(_pct(durs, 90), 2) if durs else None,
        "mttr_p1p2_median": round(statistics.median(p1p2_durs), 2) if p1p2_durs else None,
        "monitors_fired": len(mons),
        "unowned_monitors": unowned,
    }


def _one_commenter(c):
    for k in ("handle", "user", "author", "name"):
        v = c.get(k)
        if v:
            return str(v).lstrip("@")
    cc = c.get("commenter")
    if isinstance(cc, dict):
        v = cc.get("handle") or cc.get("name")
        if v:
            return str(v).lstrip("@")
    return ""


def _comments_from_events(events):
    """Map monitor_id -> [{who, text}] from any human comments posted on alert
    events in the window. Most Datadog alerts have none, but when someone replies
    on the event stream we surface it as 'who worked on it'."""
    out = {}
    for e in events:
        cm = e.get("comments")
        if not cm:
            continue
        mid = e.get("monitor_id")
        for c in cm:
            if not isinstance(c, dict):
                continue
            who = _one_commenter(c)
            text = (c.get("comment") or c.get("text") or c.get("message")
                    or c.get("body") or "")
            text = str(text).strip()
            if not (who or text):
                continue
            out.setdefault(mid, []).append({"who": who, "text": text[:200]})
    return out


def _clean_title(title):
    """Strip the leading [Px] and [Status ...] boilerplate to a readable name.
    Datadog alert titles look like:
      "[P3] [Triggered on {env:beta}] [TM Image Collector] Errors Detected"
    We drop the priority and status brackets, keeping the descriptive remainder.
    """
    t = _PRIORITY_RE.sub("", title or "")

    def repl(m):
        first = (m.group(1).strip().split() or [""])[0]
        return "" if first in _STATUS_TOKENS else m.group(0)

    t = re.sub(r"\[([^\[\]]*)\]", repl, t)
    return " ".join(t.split()).strip()


async def fetch_alert_history(client, site, tag, window_days=30, limit=25):
    """Return recent monitor-alert cycles for a service tag, with time-to-resolve
    and a manual/auto resolution classification.

    - Pulls monitor alert *events* (sources=alert) and pairs each Triggered
      (alert_type error/warning) with the next Recovered (success) event for the
      same (monitor, group) to compute resolution duration.
    - Resolution type: 'manual' if the monitor is configured with
      is_manual_resolve (a human must clear it), else 'auto'.
    """
    site = site.rstrip("/")
    H = {"DD-API-KEY": DD_API_KEY, "DD-APPLICATION-KEY": DD_APP_KEY}
    app = app_base(site)
    now = int(time.time())
    win = int(window_days) * 86400
    start_cur = now - win           # start of the current window
    start = now - 2 * win           # start of the *prior* window (for trends)

    # Monitor map (id -> name/manual/priority). Options come back in the list.
    mmap = {}
    try:
        mr = await client.get(f"{site}/api/v1/monitor", headers=H,
                              params={"monitor_tags": tag, "page_size": 1000})
        if mr.status_code == 200:
            for m in mr.json() or []:
                o = m.get("options") or {}
                notify = _handles(m.get("message"))
                mmap[m.get("id")] = {"name": m.get("name"),
                                     "manual": bool(o.get("is_manual_resolve")),
                                     "priority": m.get("priority"),
                                     "notify": notify,
                                     "owners": _owner_list(notify, m.get("creator"))}
    except Exception:
        pass

    # The v1 events API rejects any single query spanning more than ~30 days
    # (HTTP 400). Split the requested window into <=30-day chunks and fetch them
    # concurrently, then merge.
    CHUNK = 30 * 86400
    segments = []
    seg_start = start
    while seg_start < now:
        seg_end = min(seg_start + CHUNK, now)
        segments.append((seg_start, seg_end))
        seg_start = seg_end

    async def _chunk(seg):
        er = await client.get(f"{site}/api/v1/events", headers=H,
                              params={"start": seg[0], "end": seg[1], "tags": tag,
                                      "sources": "alert", "unaggregated": "true"})
        er.raise_for_status()
        return er.json().get("events", []) or []

    events = []
    for part in await asyncio.gather(*(_chunk(s) for s in segments)):
        events.extend(part)

    comments_by_mid = _comments_from_events(events)

    # Pair triggered -> recovered per (monitor_id, group), chronologically.
    cycles = []
    open_by = {}
    for e in sorted(events, key=lambda x: x.get("date_happened", 0)):
        at = e.get("alert_type")
        if at not in ("error", "warning", "success"):
            continue
        mid = e.get("monitor_id")
        grp = (e.get("monitor_groups") or ["*"])[0]
        key = (mid, grp)
        if at in ("error", "warning"):
            cur = open_by.get(key)
            if not cur or cur.get("resolved_at") is not None:
                info = mmap.get(mid) or {}
                env, is_prod = _env_of(grp, e.get("title"))
                cyc = {"monitor_id": mid, "group": grp,
                       "name": _clean_title(e.get("title")) or info.get("name") or "(monitor)",
                       "priority": _parse_priority(e.get("title"), info.get("priority")),
                       "triggered_at": e.get("date_happened"),
                       "resolved_at": None,
                       "severity": "warn" if at == "warning" else "alert",
                       "manual": bool(info.get("manual")),
                       "env": env, "is_prod": is_prod,
                       "has_notify": bool(info.get("notify")),
                       "owners": info.get("owners") or []}
                cycles.append(cyc)
                open_by[key] = cyc
            elif at == "error":
                cur["severity"] = "alert"  # escalate warn -> alert
        else:  # success -> recovery closes the open cycle
            cur = open_by.get(key)
            if cur and cur.get("resolved_at") is None:
                cur["resolved_at"] = e.get("date_happened")

    for c in cycles:
        if c["resolved_at"]:
            dur_h = round((c["resolved_at"] - c["triggered_at"]) / 3600.0, 2)
            c["duration_hours"] = max(dur_h, 0.0)
            c["status"] = "resolved"
            c["resolve_type"] = "manual" if c["manual"] else "auto"
        else:
            c["duration_hours"] = None
            c["status"] = "open"
            c["resolve_type"] = None
        c["monitor_url"] = f"{app}/monitors/{c['monitor_id']}" if c.get("monitor_id") else None

    cycles.sort(key=lambda c: c.get("triggered_at") or 0, reverse=True)

    # Split into current vs prior window (for trend deltas). Anything still open
    # counts as *current* exposure regardless of when it first fired.
    cur = [c for c in cycles
           if (c.get("triggered_at") or 0) >= start_cur or c["status"] == "open"]
    prev = [c for c in cycles
            if c["status"] == "resolved" and start <= (c.get("triggered_at") or 0) < start_cur]

    def _slim(c):
        return {k: c.get(k) for k in ("monitor_id", "priority", "duration_hours",
                                      "status", "resolve_type", "is_prod", "env",
                                      "has_notify")}

    resolved = [c for c in cur if c["status"] == "resolved"]
    still_open = [c for c in cur if c["status"] == "open"]

    # ---- Top alerts: aggregate per monitor, keep those that are open, resolved
    # manually, or occurring frequently, and rank them (open > manual > frequent).
    FREQUENT = 3  # >= this many fires in the window counts as "occurring frequently"
    agg = {}
    for c in cur:
        k = c.get("monitor_id") or c["name"]
        a = agg.get(k)
        if not a:
            a = {"monitor_id": c.get("monitor_id"), "name": c["name"],
                 "priority": c.get("priority"), "owners": c.get("owners") or [],
                 "monitor_url": c.get("monitor_url"),
                 "env": c.get("env"), "is_prod": c.get("is_prod"),
                 "count": 0, "open_count": 0, "manual_resolved": 0,
                 "auto_resolved": 0, "last_triggered": 0, "_durs": []}
            agg[k] = a
        a["count"] += 1
        a["last_triggered"] = max(a["last_triggered"], c.get("triggered_at") or 0)
        if c["status"] == "open":
            a["open_count"] += 1
        elif c["resolve_type"] == "manual":
            a["manual_resolved"] += 1
        elif c["resolve_type"] == "auto":
            a["auto_resolved"] += 1
        if c.get("duration_hours") is not None:
            a["_durs"].append(c["duration_hours"])
        cp = c.get("priority")
        if cp and (not a["priority"] or cp < a["priority"]):
            a["priority"] = cp
        if c.get("owners") and not a["owners"]:
            a["owners"] = c["owners"]

    top = []
    for a in agg.values():
        reasons = []
        if a["open_count"]:
            reasons.append("open")
        if a["manual_resolved"]:
            reasons.append("manual")
        if a["count"] >= FREQUENT:
            reasons.append("frequent")
        if not reasons:
            continue  # only surface open / manually-resolved / frequent alerts
        a["reasons"] = reasons
        a["mttr_hours_median"] = round(statistics.median(a["_durs"]), 2) if a["_durs"] else None
        a["score"] = a["open_count"] * 1_000_000 + a["manual_resolved"] * 10_000 + a["count"]
        a["comments"] = comments_by_mid.get(a["monitor_id"], [])[:5]
        a.pop("_durs", None)
        top.append(a)
    # Rank by priority (P1 first), then attention score, then most recent.
    top.sort(key=lambda a: (a.get("priority") or 99, -a["score"], -(a["last_triggered"] or 0)))

    summary = _summarize(cur)
    return {"summary": summary,
            "alerts": cur[:limit], "top": top,
            "cycles": [_slim(c) for c in cur],
            "cycles_prev": [_slim(c) for c in prev],
            "manage_url": _manage_url(app, tag)}


def _pct(values, p):
    if not values:
        return 0.0
    xs = sorted(values)
    k = (len(xs) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)
