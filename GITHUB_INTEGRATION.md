# GitHub Integration Guide

## Overview

The enhanced GitHub adapter provides comprehensive software delivery metrics that feed into the onboarding readiness assessment. It supports **multiple repositories per service** with aggregated metrics and deployment frequency tracking.

## Configuration

### Per-Service GitHub Setup

Add a `github` block to any service in `projects.json`:

```json
{
  "name": "TM AI Service",
  "jira_scope": "TM AI Service",
  "security_scope": "task-mining-ai",
  "github": {
    "repos": [
      "owner/repo-name",
      "owner/another-repo"
    ]
  }
}
```

### Global Defaults

The `defaults.github` block in `projects.json` sets site and sampling parameters:

```json
"github": {
  "site": "https://api.github.com",
  "per_repo_sample_days": 90
}
```

For GitHub Enterprise, override the site URL in the service config:

```json
"github": {
  "repos": ["owner/repo"],
  "site": "https://github.enterprise.com/api/v3"
}
```

## Metrics Collected

### Repository-Level Metrics

- **open_prs**: Count of open pull requests
- **merged_prs_sampled**: Number of merged PRs in the sample window
- **median_merge_hours**: Time from PR creation to merge (50th percentile)
- **reviewed_pct**: Percentage of PRs that received reviews before merge
- **branch_protection**: Whether the default branch has protection enabled
- **commits_per_week**: Average weekly commit frequency (over last 90 days)
- **releases_per_week**: Average weekly release/tag frequency
- **deployment_frequency**: Categorical assessment (`high`/`medium`/`low`) based on release frequency

### Aggregated Metrics (Multi-Repo)

All repos for a service are aggregated:

- **repos_monitored**: Number of repos contributing to this service
- **open_prs**: Sum across all repos
- **median_merge_hours**: Median across repos
- **reviewed_pct**: Mean review percentage
- **branch_protection_all**: True only if ALL repos have protection
- **avg_commits_per_week**: Mean commit frequency
- **avg_releases_per_week**: Mean release frequency
- **deployment_frequency**: Inferred from repo frequencies (`high` if ≥80% of repos are high, etc.)
- **repo_details**: Array with per-repo breakdown

## Scoring & Gates

The metrics feed into readiness tiers via the following gates:

### Critical Gaps (→ RED)
- `branch_protection_all = false`: No protection on main branch across all repos

### Standard Gaps (→ AMBER, then DEFERRED if ≥7)
- Median merge time > 16 hours
- PR review coverage < 85%
- Deployment frequency below target (`medium` or higher)
- Commit frequency < 2 commits/week (sign of abandonment)
- No repos monitored (incomplete config)

## Thresholds

Customizable per service via `thresholds` override in `projects.json`:

```json
{
  "name": "Service Name",
  "github": { "repos": ["..."] },
  "thresholds": {
    "merge_hours_max": 12,
    "reviewed_pct_min": 90,
    "deployment_frequency_min": "high",
    "commits_per_week_min": 5
  }
}
```

**Default thresholds:**
- `merge_hours_max`: 16 hours
- `reviewed_pct_min`: 85%
- `deployment_frequency_min`: "medium" (weekly or more)
- `commits_per_week_min`: 2 commits

## API Response Shape

```json
{
  "github": {
    "open_prs": 6,
    "repos_monitored": 2,
    "median_merge_hours": 5.2,
    "reviewed_pct": 92,
    "branch_protection": true,
    "branch_protection_all": true,
    "avg_commits_per_week": 8.3,
    "avg_releases_per_week": 0.5,
    "deployment_frequency": "medium",
    "repo_details": [
      {
        "repo": "owner/repo1",
        "open_prs": 3,
        "merged_prs_sampled": 12,
        "median_merge_hours": 4.5,
        "reviewed_pct": 95,
        "branch_protection": true,
        "commits_per_week": 8.5,
        "releases_per_week": 0.4,
        "deployment_frequency": "medium"
      },
      {
        "repo": "owner/repo2",
        "open_prs": 3,
        "merged_prs_sampled": 14,
        "median_merge_hours": 5.9,
        "reviewed_pct": 89,
        "branch_protection": true,
        "commits_per_week": 8.1,
        "releases_per_week": 0.6,
        "deployment_frequency": "medium"
      }
    ]
  }
}
```

## Error Handling

- **Missing GITHUB_TOKEN**: Returns `"not_configured"` in `source_status`
- **Bad token / permissions**: Returns `"error: 401/403: ..."`
- **Repo not found**: Returns `"error: 404: ..."`
- **Partial failure**: If some repos fail but others succeed, aggregates successful repos and includes failed repos in error details
- **GraphQL unavailable**: Falls back to REST API

## Authentication

Set `GITHUB_TOKEN` in `.env`:

```bash
GITHUB_TOKEN=ghp_xxxxxxxxxxxx
```

**Required scopes:**
- `repo` (read access to public/private repos)
- `read:org` (for org-wide metrics, optional)

For GitHub Enterprise:
```bash
GITHUB_ENTERPRISE_TOKEN=ghp_xxxxxxxxxxxx
GITHUB_ENTERPRISE_HOST=github.enterprise.com
```

## Best Practices

1. **Group related repos**: If a service spans multiple repos (e.g., frontend + backend), list them all so metrics are aggregated
2. **Use consistent naming**: Keep repo names aligned with security scopes for easy tracing
3. **Check branch protection**: Ensure all repos have default-branch protection before going live
4. **Set appropriate thresholds**: Adjust `deployment_frequency_min` based on your release cadence (e.g., `high` for SaaS, `low` for libraries)
5. **Monitor repo_details**: The per-repo breakdown helps spot outliers (one repo with low review coverage, etc.)

## Troubleshooting

### No GitHub data appearing
- Check `GITHUB_TOKEN` is set and valid: `curl -H "Authorization: Bearer $GITHUB_TOKEN" https://api.github.com/user`
- Verify repo names are in `owner/repo` format
- Check `source_status.github` in the API response for error details

### Branch protection showing false
- Confirm the branch protection rule exists on the default branch
- Check GitHub token has sufficient permissions (may need admin scope)

### Low review percentage
- Verify reviews are being recorded (GitHub counts approval/request-changes/comment)
- Check if PRs are merged by automation (bots may not show as "reviewed")

### Deployment frequency showing "low"
- Releases must be tagged or use GitHub Releases API
- Check that commits are being pushed regularly (commits_per_week)

## Future Enhancements

- GraphQL-based metrics for better performance on large orgs
- Automated remediation suggestions (enable branch protection, etc.)
- Integration with GitHub Actions for CI/CD metrics
- Security scanning results (Dependabot, CodeQL)
- Code coverage trends
- Contributor churn analysis
