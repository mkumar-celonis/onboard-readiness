"""Mock GitHub adapter for testing/demo — returns realistic test data."""
import random

async def fetch(client, cfg):
    """Return realistic mock GitHub metrics for all repos in config."""
    repos = cfg.get("repos", [])
    if not repos:
        raise RuntimeError("No repos configured in GitHub config")

    # Generate mock metrics for each repo
    all_metrics = []
    for repo in repos:
        # Realistic but varied metrics per repo
        merge_hours = random.uniform(3, 10)
        reviewed_pct = random.randint(85, 99)
        commits_pw = random.uniform(5, 15)
        releases_pw = random.choice([0.3, 0.5, 1.0, 1.5])

        metrics = {
            "repo": repo,
            "open_prs": random.randint(2, 8),
            "merged_prs_sampled": random.randint(10, 30),
            "median_merge_hours": round(merge_hours, 1),
            "reviewed_pct": reviewed_pct,
            "branch_protection": True,  # Good practice
            "commits_per_week": round(commits_pw, 1),
            "releases_per_week": round(releases_pw, 2),
            "deployment_frequency": (
                "high" if releases_pw >= 1
                else "medium" if releases_pw >= 0.25
                else "low"
            ),
        }
        all_metrics.append(metrics)

    # Aggregate across repos
    def freq_rank(f):
        return {"high": 2, "medium": 1, "low": 0, "unknown": -1}.get(f, -1)

    aggregated = {
        "open_prs": sum(m.get("open_prs", 0) for m in all_metrics),
        "repos_monitored": len(all_metrics),
        "median_merge_hours": round(
            sum(m.get("median_merge_hours", 0) for m in all_metrics) / max(len(all_metrics), 1), 1
        ),
        "reviewed_pct": round(
            sum(m.get("reviewed_pct", 0) for m in all_metrics) / max(len(all_metrics), 1)
        ),
        "branch_protection": all(m.get("branch_protection", False) for m in all_metrics),
        "branch_protection_all": all(m.get("branch_protection", False) for m in all_metrics),
        "avg_commits_per_week": round(
            sum(m.get("commits_per_week", 0) for m in all_metrics) / max(len(all_metrics), 1), 1
        ),
        "avg_releases_per_week": round(
            sum(m.get("releases_per_week", 0) for m in all_metrics) / max(len(all_metrics), 1), 2
        ),
        "deployment_frequency": (
            "high"
            if sum(freq_rank(m.get("deployment_frequency")) for m in all_metrics) / len(all_metrics) >= 1.5
            else "medium" if sum(freq_rank(m.get("deployment_frequency")) for m in all_metrics) / len(all_metrics) >= 0.5
            else "low"
        ),
        "repo_details": all_metrics,
    }

    return {"github": aggregated}
