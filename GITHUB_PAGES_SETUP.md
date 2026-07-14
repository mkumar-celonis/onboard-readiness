# GitHub Pages Deployment

The application is deployed to GitHub Pages at:  
**https://mkumar-celonis.github.io/onboard-readiness/**

## What's Deployed

- **Static HTML dashboard** with mock Jira + GitHub metrics
- **Real-time visualizations** of service readiness tiers
- **Interactive metrics display** showing project health

## Building for GitHub Pages

The static site is built from `docs/` folder:

```bash
python3 build-static.py
```

This creates:
- `docs/index.html` - Main dashboard
- `docs/.nojekyll` - Disables Jekyll processing (GitHub Pages default)

## Configuration

1. Go to repository **Settings** → **Pages**
2. Select **Deploy from a branch**
3. Choose **main** branch and **docs** folder
4. Save

The site will be live at: `https://<username>.github.io/onboard-readiness/`

## Current Data

The deployed version uses **mock data** for demo purposes:
- Task Mining AI Service (GREEN tier)
- Sample Jira metrics (15 items, 8 in progress)
- Sample GitHub metrics (6 open PRs, 5.2h merge time)

## Moving to Live Data

To connect to real Jira + GitHub APIs:

1. Update `build-static.py` to call the FastAPI backend during build
2. Store API credentials in GitHub Secrets
3. Use GitHub Actions to rebuild on schedule or on-demand

Example workflow:
```yaml
name: Update Dashboard
on:
  schedule:
    - cron: '0 * * * *'  # Hourly
  workflow_dispatch:

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
      - run: |
          export JIRA_TOKEN=${{ secrets.JIRA_TOKEN }}
          export GITHUB_TOKEN=${{ secrets.GITHUB_TOKEN }}
          python3 build-static.py
      - uses: stefanzweifel/git-auto-commit-action@v4
```

## File Structure

```
docs/
├── index.html       # Static dashboard
└── .nojekyll       # GitHub Pages config
```

## Troubleshooting

**Page not loading?**
- Check repository is public (or deploy key is set for private)
- Wait 1-2 minutes for GitHub Pages to rebuild
- Clear browser cache (Ctrl+Shift+Del)

**Metrics not updating?**
- Rebuild using `python3 build-static.py`
- Commit and push to main branch

**Custom domain?**
- Add `docs/CNAME` with your domain
- Update DNS to point to `mkumar-celonis.github.io`
