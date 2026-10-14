# percona-pg-activity

Daily-updated chart of PostgreSQL commits and reviews per company for 2026,
generated from the [postgres/postgres](https://github.com/postgres/postgres) git log.

**Live page:** https://percona-lab.github.io/percona-pg-activity/

## How it works

`.github/workflows/update.yml` runs every day at 05:17 UTC (and on every push to `main`,
or manually via *Actions → Update PG activity chart → Run workflow*). It:

1. clones the PostgreSQL commit history (commits only, no file contents),
2. runs `percona_activity.py --after 2025-12-31 --company-statistics --company-chart`,
3. publishes `index.html`, `chart.svg` and `stats.md` to GitHub Pages.

## Updating company attribution

Edit `COMPANY_DOMAINS` / the name-to-company mappings / `MANUAL_IDENTITIES` in
`percona_activity.py` and push; the page rebuilds automatically.

## Running locally

```
cd /path/to/postgres
python3 /path/to/percona_activity.py --after 2025-12-31 --company-statistics --company-chart
```

Requires Python 3.10+, no extra packages.
