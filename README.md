# percona-pg-activity

A dashboard of Percona's contributions to PostgreSQL, rebuilt daily from the
[postgres/postgres](https://github.com/postgres/postgres) git history.

**Live page:** https://percona-lab.github.io/percona-pg-activity/

It shows, for 2026 (with "last 6 / 3 / 1 months" filters):

- headline numbers: changes authored and reviewed, rank among companies, share of all
  PostgreSQL changes, changes landing in the next major release, fixes backpatched to
  released versions
- monthly activity, the company ranking, per-engineer credits and the areas of
  PostgreSQL we work in
- every change with Percona credit: what it does (from the commit message), who
  authored/reviewed/reported it, which PostgreSQL versions it ships in, and links to the
  commit and mailing-list discussion; searchable, filterable, and exportable as Markdown
  or CSV

## How it works

`.github/workflows/update.yml` runs daily at 05:17 UTC, on every push to `main`, or
manually (*Actions → Update PG activity dashboard → Run workflow*). It:

1. clones PostgreSQL's recent history (commits and trees, no file contents),
2. runs `build_dashboard_data.py` → `data.json`, which `index.html` renders,
3. also runs `percona_activity.py --company-statistics --company-chart` → `chart.svg`
   and `stats.md` (the original chart and Markdown tables),
4. publishes everything to GitHub Pages.

| File | Purpose |
|---|---|
| `percona_activity.py` | Identity and company attribution (`COMPANY_DOMAINS`, `COMPANY_NAME_OVERRIDES`, `MANUAL_IDENTITIES`); also a standalone CLI report |
| `build_dashboard_data.py` | Builds `data.json`: per-change details, versions, areas, categories, monthly company counts |
| `index.html` | The dashboard (plain HTML/JS, no dependencies) |

## Updating attribution

Someone missing, or attributed to the wrong company? Edit the mappings in
`percona_activity.py` and push; the page rebuilds within a few minutes.
Change types ("Bug fix", "New feature", …) and areas are heuristics from the commit
subject and changed paths: see `CATEGORY_RULES` and `AREA_RULES` in
`build_dashboard_data.py`.

## Running locally

```
cd /path/to/postgres            # needs refs/remotes/origin/{master,REL_*_STABLE}
python3 /path/to/build_dashboard_data.py --after 2025-12-31 --output /tmp/site/data.json
cp /path/to/index.html /tmp/site/ && python3 -m http.server -d /tmp/site 8000
```

Requires Python 3.10+, no extra packages.

## Roadmap

The page is meant to grow into a single Percona PostgreSQL community dashboard:
talks at PostgreSQL events, blog posts (percona.com and percona.community),
mailing-list activity, and extension/ecosystem work.
