# GitHub Integration Migration Guide

## For Existing Implementations

If you have an existing GitHub configuration in `projects.json` at the **project level**, here's how to migrate to the new **service-level configuration**.

## Old Pattern (Project-Level)

```json
{
  "key": "TMT",
  "name": "Task Mining",
  "github": {
    "repos": ["celonis/task-mining-client"]
  },
  "services": [
    { "name": "TM Client", "jira_scope": "TM Client" },
    { "name": "TM Gateway", "jira_scope": "TM Gateway" }
  ]
}
```

**Problem:** All services reported the same GitHub metrics (from one repo).

## New Pattern (Service-Level)

```json
{
  "key": "TMT",
  "name": "Task Mining",
  "services": [
    {
      "name": "TM Client",
      "jira_scope": "TM Client",
      "security_scope": "task-mining-client",
      "github": {
        "repos": ["celonis/task-mining-client"]
      }
    },
    {
      "name": "TM Gateway",
      "jira_scope": "TM Gateway",
      "security_scope": "task-mining-gateway",
      "github": {
        "repos": ["celonis/task-mining-gateway"]
      }
    },
    {
      "name": "TM Uploader",
      "jira_scope": "TM Uploader",
      "security_scope": "task-mining-uploader",
      "github": {
        "repos": [
          "celonis/task-mining-uploader-service",
          "celonis/task-mining-uploader-ui"
        ]
      }
    }
  ]
}
```

**Benefits:**
- Each service gets its own GitHub metrics
- Support multiple repos per service
- Aggregation is transparent and visible in API response
- Enables per-service thresholds

## Migration Steps

### 1. Identify Your Repo Mapping

Create a mapping of services to their GitHub repos:

```
TM Client        → celonis/task-mining-client
TM Gateway       → celonis/task-mining-gateway
TM Uploader      → celonis/task-mining-uploader-service, celonis/task-mining-uploader-ui
TM Chrome Ext    → celonis/task-mining-chrome-extension
TM AI Service    → celonis/task-mining-ai (+ any ML repos)
```

**Tips:**
- Ask your engineering leads which repos belong to each service
- Check the service's component description in Jira (usually lists repos)
- Look at the CODEOWNERS file in main repo for service-to-repo mapping
- Use GitHub org's team structure as a reference

### 2. Add `github` Block to Each Service

For each service, add a `github` object with its `repos` array:

```json
{
  "name": "TM Chrome Extension",
  "jira_scope": "TM Chrome Extension",
  "security_scope": "task-mining-chrome-extension",
  "github": {
    "repos": ["celonis/task-mining-chrome-extension"]
  }
}
```

### 3. Remove Project-Level GitHub (if present)

If you have a project-level `github` block, remove it:

```json
{
  "key": "TMT",
  "name": "Task Mining",
  // ❌ DELETE THIS:
  // "github": { "repos": ["..."] },
  "services": [
    // Service-level configs now
  ]
}
```

### 4. Add Custom Thresholds (Optional)

For services with specific deployment cadences, override thresholds:

```json
{
  "name": "TM Chrome Extension",
  "github": { "repos": ["celonis/task-mining-chrome-extension"] },
  "thresholds": {
    "deployment_frequency_min": "high",
    "merge_hours_max": 8,
    "commits_per_week_min": 10
  }
}
```

**When to customize:**
- Fast-moving services: lower `merge_hours_max`, higher `deployment_frequency_min`
- Maintenance repos: higher `merge_hours_max`, lower `commits_per_week_min`
- Batch-released components: `deployment_frequency_min": "low"`

### 5. Validate Configuration

```bash
# Check JSON is valid
python3 -c "import json; json.load(open('projects.json'))" && echo "✓ Valid"

# Quick test with curl
curl -s http://localhost:8000/api/report/TMT | jq '.services[0].github'
```

### 6. Test the Dashboard

1. Start the service: `./run.sh`
2. Open http://localhost:8000
3. Look for the project in the portfolio
4. Click on a service and check:
   - GitHub metrics appear in the report
   - `repo_details` shows all repos for that service
   - Tier reflects new GitHub gates

## Rollback

If you need to revert:

1. Restore projects.json from git: `git checkout projects.json`
2. Restart the service
3. Old single-repo behavior resumes

## FAQ

### Q: What if a service doesn't have any GitHub repos?
**A:** Leave the `github` block out. GitHub metrics will show as `null` and won't affect the tier (only if not configured).

### Q: Can I have multiple services sharing the same repo?
**A:** Yes, but be aware:
- Metrics (commits, PRs) will be counted multiple times in aggregates
- Consider whether the repo is truly multi-service (monorepo?) or should be split
- Best practice: one service per repo (or one service managing multiple related repos)

### Q: Should I list every repo or only the "main" ones?
**A:** Include all repos that contain code for that service:
- Main service code ✅
- Supporting/client libraries ✅
- Build/deployment helpers ✅
- Documentation repos ❌ (unless they contain runnable code)
- Archived/inactive repos ❓ (ask your team)

### Q: How do I know if my mapping is correct?
**A:** Check against your deployment pipeline:
- What repos does the CI/CD deploy for this service?
- Where does the on-call runbook point?
- What repos can the service team commit to?

### Q: My service has 5 repos — will that hurt performance?
**A:** No. Each repo makes ~3-4 API calls (paginated). 5 repos = ~15-20 calls per service per report refresh. GitHub API allows 60 requests/min with token, so you're fine.

### Q: Can I override the sample window (90 days)?
**A:** Not per-service yet. Modify `per_repo_sample_days` in `defaults.github` in projects.json:

```json
"defaults": {
  "github": {
    "site": "https://api.github.com",
    "per_repo_sample_days": 180  // 6 months instead of 90 days
  }
}
```

### Q: What if I move a repo to a different org?
**A:** Update the repo reference:
```json
"repos": ["old-org/repo"]  // ❌
"repos": ["new-org/repo"]  // ✅
```
The GITHUB_TOKEN must have access to both orgs.

## Common Patterns

### Monorepo with Multiple Services

If your org uses a monorepo (e.g., `celonis/platform`), map each service to its directory:

```json
{
  "name": "TM Client",
  "github": {
    "repos": ["celonis/platform"]  // Service slice lives here
  }
}
```

The adapter will count all commits/PRs to the monorepo. For finer-grained tracking, you'd need branch-based filtering (future enhancement).

### Microservices Architecture

Each service has its own repo:

```json
{
  "name": "Auth Service",
  "github": { "repos": ["celonis/auth-service"] }
},
{
  "name": "API Gateway",
  "github": { "repos": ["celonis/api-gateway"] }
}
```

### Distributed Team with Satellite Repos

A service spans multiple repos (core + plugins + client libs):

```json
{
  "name": "TM Cloud",
  "github": {
    "repos": [
      "celonis/tm-cloud-backend",
      "celonis/tm-cloud-frontend",
      "celonis/tm-cloud-sdk-js",
      "celonis/tm-cloud-cli"
    ]
  }
}
```

Metrics aggregate across all four repos.

## Support

- **Documentation**: See GITHUB_INTEGRATION.md
- **Issues**: Check GITHUB_INTEGRATION.md § Troubleshooting
- **Custom setup**: Adjust thresholds per-service in projects.json
