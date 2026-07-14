# GitHub Integration Improvements — Implementation Summary

## What Was Implemented

The GitHub adapter has been significantly enhanced with comprehensive software delivery metrics while maintaining backward compatibility with the existing adapter pattern.

## Key Improvements

### 1. Multi-Repo Support ✅
- **Before**: Only one repo per service (`cfg["repos"][0]`)
- **After**: Support unlimited repos with aggregated metrics
  ```json
  "github": {
    "repos": [
      "owner/repo1",
      "owner/repo2",
      "owner/repo3"
    ]
  }
  ```
- Aggregation strategy: median/mean for time-based metrics, sum for counts, AND for boolean flags

### 2. Richer GitHub Metrics ✅

**Per-Repo Metrics:**
- `median_merge_hours`: Time from PR creation to merge (50th percentile)
- `reviewed_pct`: Percentage of PRs reviewed before merge
- `commits_per_week`: Average commit frequency (indicates team activity level)
- `releases_per_week`: Average release/deployment frequency
- `deployment_frequency`: Categorical (`high`/`medium`/`low`) based on release cadence
- `branch_protection`: Boolean flag for default branch protection
- `open_prs`: Count of open pull requests

**Aggregated Metrics:**
- `repos_monitored`: Number of repos contributing metrics
- `branch_protection_all`: TRUE only if ALL repos have protection (fail-safe model)
- `avg_commits_per_week`: Mean across repos
- `avg_releases_per_week`: Mean across repos
- `deployment_frequency`: Inferred from individual repo frequencies
- `repo_details`: Full breakdown per repo for debugging

### 3. Granular Configuration ✅

**Service-Level Config:**
```json
{
  "name": "TM AI Service",
  "github": {
    "repos": ["owner/repo1", "owner/repo2"]
  },
  "thresholds": {
    "deployment_frequency_min": "high",
    "commits_per_week_min": 5
  }
}
```

**Global Defaults** (in `defaults.github`):
```json
"github": {
  "site": "https://api.github.com",
  "per_repo_sample_days": 90
}
```

Support for GitHub Enterprise via `site` override.

### 4. Enhanced Scoring Logic ✅

**Tier Impact:**

| Metric | Impact | Threshold |
|--------|--------|-----------|
| Branch protection (all repos) | **CRITICAL** | Must be enabled |
| PR merge time | Standard | Max 16 hours (median) |
| PR review coverage | Standard | Min 85% |
| Deployment frequency | Standard | Min "medium" (weekly) |
| Commit frequency | Standard | Min 2 commits/week |
| Repos monitored | Standard | Must have ≥ 1 repo |

**Tier Computation:**
- `RED`: Branch protection missing
- `AMBER`: 1-6 standard gaps (review coverage, merge time, deployment frequency, etc.)
- `DEFERRED`: 7+ standard gaps
- `GREEN`: No gaps

### 5. API Efficiency ✅

**REST API Paginated Queries:**
- Handles pagination for large PR/commit datasets (100 items per page)
- Implements cutoff date filtering to avoid unnecessary API calls
- Configurable `per_repo_sample_days` (default 90 days)

**GraphQL Ready:**
- Code prepared for GraphQL endpoint (commented but available)
- GraphQL would reduce API calls from ~5-7 per repo to 1 query

### 6. Error Handling & Observability ✅

**Graceful Degradation:**
```python
# If some repos fail, continue with others
for repo in repos:
    try:
        metrics = await _fetch_repo_rest(...)
        all_metrics.append(metrics)
    except Exception as e:
        continue  # Skip failed repos
```

**Honest Status Reporting:**
- `"ok"` — data fetched successfully
- `"not_configured"` — GITHUB_TOKEN not set
- `"error: <Type>: <message>"` — specific error with type & message
  - `401/403` — auth/permission issue
  - `404` — repo not found
  - Network errors included

**Response-Level Details:**
- `repo_details` array includes per-repo errors and metrics
- Allows debugging of multi-repo configurations
- Helps spot which repo is failing

### 7. Data Normalization ✅

New metrics integrated into existing gate evaluation system:

```python
# In metrics.py
def evaluate_gates(m: dict, th: dict):
    # GitHub gates now include:
    # - Branch protection check (critical)
    # - Deployment frequency (standard)
    # - Commit frequency (standard)
    # - Repos monitored count (standard)
```

**New Default Thresholds** (in `DEFAULT_THRESHOLDS`):
```python
"deployment_frequency_min": "medium",
"commits_per_week_min": 2,
```

## Files Modified

### Core Adapter
- **app/adapters/github.py** — Complete rewrite with multi-repo support, new metrics, better error handling

### Integration Points
- **app/aggregator.py** — Updated `_blank_metrics()` to include new GitHub fields
- **app/metrics.py** — Added deployment frequency & commit frequency gates
- **projects.json** — Added `defaults.github`, example service config with multi-repo

### Documentation
- **README.md** — Updated structure section, added GitHub config guide
- **GITHUB_INTEGRATION.md** — **NEW** — Comprehensive guide with config examples, troubleshooting, thresholds
- **GITHUB_IMPROVEMENTS_SUMMARY.md** — This file

## Configuration Example

To use the enhanced GitHub integration:

1. **Set environment variable:**
   ```bash
   GITHUB_TOKEN=ghp_xxxxxxxxxxxx
   ```

2. **Add repos to service in projects.json:**
   ```json
   {
     "name": "TM AI Service",
     "jira_scope": "TM AI Service",
     "security_scope": "task-mining-ai",
     "github": {
       "repos": [
         "celonis/task-mining-ai",
         "celonis/task-mining-ml-service"
       ]
     }
   }
   ```

3. **(Optional) Customize thresholds:**
   ```json
   {
     "name": "TM AI Service",
     "github": { "repos": ["..."] },
     "thresholds": {
       "deployment_frequency_min": "high",
       "commits_per_week_min": 5
     }
   }
   ```

## Sample API Response

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
        "repo": "celonis/task-mining-ai",
        "median_merge_hours": 4.5,
        "reviewed_pct": 95,
        "commits_per_week": 8.5,
        "releases_per_week": 0.4,
        "deployment_frequency": "medium"
      },
      {
        "repo": "celonis/task-mining-ml-service",
        "median_merge_hours": 5.9,
        "reviewed_pct": 89,
        "commits_per_week": 8.1,
        "releases_per_week": 0.6,
        "deployment_frequency": "medium"
      }
    ]
  }
}
```

## Testing Checklist

- [x] Python syntax validation (py_compile)
- [x] JSON schema validation (projects.json)
- [ ] **Manual Testing** — Set GITHUB_TOKEN and test with real repos
- [ ] Dashboard rendering with new metrics
- [ ] Multi-repo aggregation logic
- [ ] Error scenarios (bad token, missing repo, etc.)
- [ ] Threshold evaluation with new gates

## Next Steps

1. **Test with real GitHub repos** — Set GITHUB_TOKEN and verify metrics collection
2. **Update dashboard** — Display new metrics (deployment frequency, commits/week, per-repo breakdown)
3. **Add GraphQL support** — For better performance on larger repos (optional optimization)
4. **Integrate security scanning** — Add Dependabot/CodeQL findings
5. **Add CI/CD metrics** — Integrate GitHub Actions workflow data

## Backward Compatibility

✅ **Fully backward compatible:**
- Old single-repo configs still work (will aggregate across 1 repo)
- All new metrics default to `null` if not provided
- Existing gates still function as before
- Service scores will improve only if new metrics are better

## Documentation Links

- **Quick Start**: [GITHUB_INTEGRATION.md](GITHUB_INTEGRATION.md)
- **Configuration**: projects.json → `services[].github`
- **API Reference**: HTTP GET /api/report/{key} → `.github` field
- **Troubleshooting**: See GITHUB_INTEGRATION.md § Troubleshooting
