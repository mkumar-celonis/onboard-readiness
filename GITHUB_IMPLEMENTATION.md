# GitHub Integration Implementation

## Code Changes

### 1. GitHub Adapter (`app/adapters/github.py`)

**Multi-Repository Support:**
- `_fetch_repo_rest()`: Fetches metrics for individual repos via GitHub REST API
  - Open PRs count
  - Merge time (median hours from creation to merge)
  - Code review coverage percentage
  - Branch protection status
  - Weekly commit frequency
  - Weekly release frequency
  
- `fetch()`: Aggregates metrics across all configured repos
  - Sums: open_prs
  - Medians: merge_hours
  - Means: review_pct, commits_per_week, releases_per_week
  - AND logic: branch_protection_all (must be true on ALL repos)
  - Infers deployment_frequency from individual repo frequencies

**Aggregation Logic:**
- Branch Protection: ALL repos must have protection (fail-safe)
- Merge Time: Median across repos (representative)
- Review Coverage: Mean across repos (average quality)
- Commits/Week: Mean across repos (average activity)
- Deployment Frequency: Inferred from % of repos with high frequency

### 2. Mock Adapter (`app/adapters/github_mock.py`)

**Purpose:** Development and testing without GitHub credentials

**Mock Data Generation:**
- Per-repo metrics with realistic variations
- Branch protection always enabled (best practice)
- Merge hours: 3-10 hours
- Review coverage: 85-99%
- Commits/week: 5-15
- Release frequencies: 0.3, 0.5, 1.0, 1.5 per week
- Same aggregation as live adapter

### 3. Configuration Schema

**projects.json update:**
```json
{
  "github": {
    "repos": [
      "owner/repo1",
      "owner/repo2"
    ]
  }
}
```

### 4. Metrics Fields Added

**Aggregated GitHub Metrics:**
- `open_prs`: Total open pull requests
- `repos_monitored`: Number of repos contributing metrics
- `median_merge_hours`: Merge time (50th percentile)
- `reviewed_pct`: Code review coverage (%)
- `branch_protection_all`: TRUE only if ALL repos protected
- `avg_commits_per_week`: Average weekly commits
- `avg_releases_per_week`: Average weekly releases
- `deployment_frequency`: HIGH/MEDIUM/LOW
- `repo_details`: Array of per-repo metrics

### 5. Scoring Integration

**Critical Gate (→ RED):**
- `branch_protection_all = false` → CRITICAL gap → RED tier

**Standard Gates (→ AMBER/DEFERRED):**
- Merge time > 16 hours
- Review coverage < 85%
- Deployment frequency = LOW
- Commits/week < 2
- No repos configured

**Gap Counting:**
- 1-6 gaps → AMBER
- 7+ gaps → DEFERRED

## Files Modified

1. `app/adapters/github.py` - Live GitHub REST API adapter
2. `app/adapters/github_mock.py` - Mock adapter for development
3. `projects.json` - Updated to support `github.repos` array

## Files Added (Documentation)

1. `GITHUB_INTEGRATION.md` - Configuration & API reference
2. `MANAGEMENT_GUIDE.md` - Manager decision framework
3. `GITHUB_MIGRATION_GUIDE.md` - Migration from v1 to v2
4. `RELEASE_NOTES.md` - Feature summary & gates
5. `TEAM_SETUP_GUIDE.md` - Quick start guide

## Testing

### With Mock Adapter
```python
from app.adapters import github_mock
metrics = await github_mock.fetch(client, {
    "repos": ["owner/repo1", "owner/repo2"]
})
```

### With Live GitHub
Set `GITHUB_TOKEN` in `.env` with scope `repo`, then use standard adapter.

## Key Design Decisions

1. **AND Logic for Branch Protection**: Fail-safe approach - all repos must be protected
2. **Median for Merge Time**: Representative metric across repos
3. **Mean for Other Metrics**: Average performance indicator
4. **Deployment Frequency Inference**: Based on % of repos at each level
5. **Per-Repo Details**: Included for troubleshooting outliers
6. **Mock Adapter**: Enable development without credentials
7. **Graceful Degradation**: GitHub section shows "not configured" if credentials missing

## Future Enhancements

- GraphQL API integration for batch queries
- Webhook-based metrics updates
- Security scanning results (Dependabot, CodeQL)
- Code coverage trends
- Contributor churn analysis
