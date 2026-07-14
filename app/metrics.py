"""Gap evaluation with ONE common threshold set for all services.

Service-tier classification was removed — it was a human judgment
(customer-facing vs internal) that couldn't be derived from Jira, so
it violated the "no manual per-project config" model. One bar for
every service now. If certain services genuinely need stricter
thresholds later, we'll revisit — but based on real data, not a label.

Only sources with status='ok' are scored; unavailable sources are honestly skipped.
"""

DEFAULT_THRESHOLDS = {
    "feature_pct_min": 80,
    "reopen_rate_max": 5,
    "merge_hours_max": 16,
    "reviewed_pct_min": 85,
    "muted_monitors_max": 3,
    "cycle_p90_max": 25,
    "deployment_frequency_min": "medium",
    "commits_per_week_min": 2,
}

TIER_RANK  = {"GREEN":0, "AMBER":1, "DEFERRED":2, "RED":3}
RANK_TIER  = {v:k for k,v in TIER_RANK.items()}


def resolve_thresholds(service_tier=None, override: dict | None = None) -> dict:
    """One threshold set for everyone. `service_tier` accepted (and ignored)
    for backward compat with callers still passing it. Per-service `override`
    can still tune specific fields for a specific service."""
    base = dict(DEFAULT_THRESHOLDS)
    if override: base.update(override)
    return base


def compute_tier(gates: dict) -> str:
    if len(gates["critical_gaps"]) > 0:      return "RED"
    if len(gates["standard_gaps"]) == 0:     return "GREEN"
    if len(gates["standard_gaps"]) <= 6:     return "AMBER"
    return "DEFERRED"


def evaluate_gates(m: dict, th: dict, service_tier=None) -> dict:
    """Score a service's metrics against the single threshold set.
    `service_tier` accepted for backward compat but has no effect on scoring."""
    critical, standard = [], []
    src = m.get("source_status", {})

    # ---------- GitHub ----------
    if src.get("github") == "ok":
        # Branch protection (critical)
        if m["github"].get("branch_protection_all") is False:
            critical.append("No branch protection on main across all repos; direct pushes allowed (2E.1)")

        # PR merge time
        mh = m["github"].get("median_merge_hours") or 0
        if mh > th["merge_hours_max"]:
            standard.append(f"Median PR merge time {mh}h high")

        # PR review coverage
        rp = m["github"].get("reviewed_pct")
        if rp is not None and rp < th["reviewed_pct_min"]:
            standard.append(f"PR review coverage {rp}% below bar")

        # Deployment frequency
        dep_freq = m["github"].get("deployment_frequency")
        freq_rank = {"high": 2, "medium": 1, "low": 0, "unknown": -1}
        min_rank = freq_rank.get(th.get("deployment_frequency_min", "medium"), 1)
        if dep_freq and freq_rank.get(dep_freq, -1) < min_rank:
            standard.append(f"Deployment frequency {dep_freq}; target is {th.get('deployment_frequency_min', 'medium')} or higher")

        # Commit frequency / activity
        commits_pw = m["github"].get("avg_commits_per_week")
        if commits_pw is not None and commits_pw < th.get("commits_per_week_min", 2):
            standard.append(f"Low commit frequency ({commits_pw} commits/week)")

        # Monitoring multiple repos
        repos = m["github"].get("repos_monitored") or 0
        if repos == 0:
            standard.append("No GitHub repos monitored")

    # ---------- Jira ----------
    strategic_fail    = False
    insufficient_jira = False
    if src.get("jira") == "ok":
        total_tix = (m.get("tickets") or {}).get("total")
        if total_tix in (None, 0):
            # No data in this slice — don't score Jira gates. Almost always means
            # the component/label doesn't match. Config fix, not a red flag.
            insufficient_jira = True
        else:
            rr = m["reopen"]["rate_pct"]
            if rr is not None and rr > th["reopen_rate_max"]:
                standard.append(f"Reopen rate {rr}% above target")
            p90 = m["cycle"]["p90_days"]
            if p90 is not None and p90 > th["cycle_p90_max"]:
                standard.append(f"p90 time-to-close {p90}d high")
            fp = m["roadmap"]["feature_pct"]
            if fp is not None and fp < th["feature_pct_min"]:
                standard.append(f"Feature share {fp}% below target ({th['feature_pct_min']}%)")

    # ---------- Datadog ----------
    if src.get("datadog") == "ok":
        muted = m["datadog"].get("muted") or 0
        if muted > th["muted_monitors_max"]:
            standard.append(f"{muted} muted monitors — observability not clean")
        # `alerting` still surfaced as a Standard gap so we don't lose the signal
        alerting = m["datadog"].get("alerting") or 0
        if alerting > 0:
            standard.append(f"{alerting} Datadog monitor{'s' if alerting>1 else ''} in Alert now")

    # NOTE: security (CBE vulnerability tickets) is intentionally NOT scored here.
    # Security debt is a compliance obligation with its own SLA clock — not an
    # onboarding gate. It used to push open Critical/High vulns and SLA breaches
    # into critical_gaps/standard_gaps, which silently turned services RED via
    # compute_tier(). That contradicted the policy (see rollup_tier note below)
    # and was removed. Security counts still surface in the dashboard for
    # awareness — they just don't affect any service's tier anymore.

    return {"critical_gaps": critical, "strategic_fail": strategic_fail,
            "standard_gaps": standard, "insufficient_jira": insufficient_jira}


def rollup_tier(service_tiers: list[str]) -> str:
    """Worst-service-wins for the project rollup."""
    if not service_tiers: return "GREEN"
    return RANK_TIER[max(TIER_RANK[t] for t in service_tiers)]


# NOTE: evaluate_project_security() was removed. Security debt is a compliance
# obligation (its own SLA clock), not an onboarding gate — it transfers to the
# receiving team as-is. Tier is now driven purely by per-service operational
# signals. Security counts still appear in "Overall Project Health" for
# awareness; they just don't affect the tier verdict.
