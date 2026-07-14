"""Live GitHub adapter with enhanced metrics for multiple repos."""
import datetime as dt, statistics, re
from ..config import GITHUB_TOKEN

API = "https://api.github.com"
GRAPHQL_ENDPOINT = f"{API}/graphql"

async def _fetch_repo_graphql(client, headers: dict, owner: str, repo: str):
    """Fetch rich metrics via GraphQL for a single repo."""
    query = """
    query($owner: String!, $repo: String!, $since: DateTime!) {
      repository(owner: $owner, name: $repo) {
        defaultBranchRef { name }
        refs(first: 1, refPrefix: "refs/heads/") { nodes { name } }
        pullRequests(first: 50, states: MERGED, orderBy: {field: UPDATED_AT, direction: DESC}) {
          nodes {
            createdAt
            mergedAt
            reviews(first: 10) { totalCount }
            commits(last: 1) { nodes { commit { committedDate } } }
            author { login }
          }
        }
        releases(first: 20, orderBy: {field: CREATED_AT, direction: DESC}) {
          nodes { publishedAt createdAt }
        }
        vulnerabilityAlerts(first: 10) {
          nodes { dismissedAt }
        }
        dependencyGraphManifests(first: 100) {
          totalCount
        }
      }
    }
    """
    variables = {
        "owner": owner,
        "repo": repo,
        "since": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=90)).isoformat()
    }
    try:
        r = await client.post(GRAPHQL_ENDPOINT, headers=headers, json={"query": query, "variables": variables})
        if r.status_code == 200:
            result = r.json()
            if result.get("errors"):
                return None
            return result.get("data", {}).get("repository")
    except Exception:
        pass
    return None


async def _fetch_repo_rest(client, headers: dict, repo: str, days_back: int = 90):
    """Fetch REST API metrics for a single repo."""
    try:
        # Open PRs
        r = await client.get(f"{API}/repos/{repo}/pulls",
                            params={"state": "open", "per_page": 100}, headers=headers)
        r.raise_for_status()
        open_prs = len(r.json())

        # Closed/merged PRs for merge time calculation
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_back)
        hrs = []
        reviewed_count = 0
        total_merged = 0

        page = 1
        while len(hrs) < 100:
            r = await client.get(f"{API}/repos/{repo}/pulls",
                                params={"state": "closed", "per_page": 100, "page": page, "sort": "updated"},
                                headers=headers)
            r.raise_for_status()
            prs = r.json()
            if not prs:
                break
            for pr in prs:
                if not pr.get("merged_at"):
                    continue
                m = dt.datetime.fromisoformat(pr["merged_at"].replace('Z', '+00:00'))
                if m < cutoff:
                    break
                c = dt.datetime.fromisoformat(pr["created_at"].replace('Z', '+00:00'))
                hrs.append((m - c).total_seconds() / 3600)
                total_merged += 1

                # Check if PR was reviewed
                reviews_r = await client.get(f"{API}/repos/{repo}/pulls/{pr['number']}/reviews",
                                            params={"per_page": 100}, headers=headers)
                if reviews_r.status_code == 200 and reviews_r.json():
                    reviewed_count += 1
            if not prs or m < cutoff:
                break
            page += 1

        # Branch protection
        r = await client.get(f"{API}/repos/{repo}", headers=headers)
        r.raise_for_status()
        branch = r.json().get("default_branch", "main")
        prot_r = await client.get(f"{API}/repos/{repo}/branches/{branch}/protection", headers=headers)
        branch_protected = prot_r.status_code == 200

        # Commit frequency
        r = await client.get(f"{API}/repos/{repo}/commits",
                           params={"since": cutoff.isoformat(), "per_page": 100},
                           headers=headers)
        commits_count = len(r.json()) if r.status_code == 200 else 0
        commits_per_week = round(commits_count / max(days_back / 7, 1), 1)

        # Deployment info (via releases as proxy)
        r = await client.get(f"{API}/repos/{repo}/releases",
                           params={"per_page": 50}, headers=headers)
        releases = r.json() if r.status_code == 200 else []
        releases_last_90 = sum(1 for rel in releases
                              if dt.datetime.fromisoformat(rel["published_at"].replace('Z', '+00:00')) >= cutoff)
        releases_per_week = round(releases_last_90 / max(days_back / 7, 1), 2)

        return {
            "open_prs": open_prs,
            "merged_prs_sampled": total_merged,
            "median_merge_hours": round(statistics.median(hrs), 1) if hrs else 0,
            "reviewed_pct": round(reviewed_count / max(total_merged, 1) * 100),
            "branch_protection": branch_protected,
            "commits_per_week": commits_per_week,
            "releases_per_week": releases_per_week,
            "deployment_frequency": "high" if releases_per_week >= 1 else "medium" if releases_per_week >= 0.25 else "low",
        }
    except Exception as e:
        raise RuntimeError(f"GitHub REST API error for {repo}: {e}")


async def fetch(client, cfg):
    """Aggregate metrics across all configured repos."""
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN not set")

    repos = cfg.get("repos", [])
    if not repos:
        raise RuntimeError("No repos configured in GitHub config")

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    # Fetch metrics for each repo
    all_metrics = []
    for repo in repos:
        try:
            metrics = await _fetch_repo_rest(client, headers, repo, days_back=90)
            metrics["repo"] = repo
            all_metrics.append(metrics)
        except Exception as e:
            # Skip failed repos, continue with others
            continue

    if not all_metrics:
        raise RuntimeError(f"Could not fetch metrics from any of {len(repos)} repos")

    # Aggregate across repos
    aggregated = {
        "open_prs": sum(m.get("open_prs", 0) for m in all_metrics),
        "repos_monitored": len(all_metrics),
        "median_merge_hours": round(statistics.median([m.get("median_merge_hours", 0) for m in all_metrics if m.get("median_merge_hours")]), 1) if all_metrics else 0,
        "reviewed_pct": round(statistics.mean([m.get("reviewed_pct", 0) for m in all_metrics if m.get("reviewed_pct")])) if all_metrics else 0,
        "branch_protection": all(m.get("branch_protection", False) for m in all_metrics),
        "branch_protection_all": all(m.get("branch_protection", False) for m in all_metrics),
        "avg_commits_per_week": round(statistics.mean([m.get("commits_per_week", 0) for m in all_metrics]), 1) if all_metrics else 0,
        "avg_releases_per_week": round(statistics.mean([m.get("releases_per_week", 0) for m in all_metrics]), 2) if all_metrics else 0,
        "deployment_frequency": _infer_deployment_frequency([m.get("deployment_frequency") for m in all_metrics]),
        "repo_details": all_metrics,
    }

    return {"github": aggregated}


def _infer_deployment_frequency(frequencies: list) -> str:
    """Infer overall deployment frequency from individual repos."""
    if not frequencies:
        return "unknown"
    high_count = sum(1 for f in frequencies if f == "high")
    pct_high = high_count / len(frequencies) * 100
    if pct_high >= 80:
        return "high"
    elif pct_high >= 40:
        return "medium"
    else:
        return "low"
