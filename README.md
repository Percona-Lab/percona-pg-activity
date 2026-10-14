# percona-pg-activity

A dashboard of Percona's contributions to PostgreSQL, rebuilt daily from public sources.

**Live page:** https://percona-lab.github.io/percona-pg-activity/

Three tabs:

- **Code contributions** (2026, filterable by month range): changes authored and reviewed,
  company ranking, engineers, areas of PostgreSQL, and every change with Percona credit
  (summary, roles, PostgreSQL versions, links), exportable as Markdown or CSV.
- **Events & community** (since 2022, by year): talks, organiser / program / Code of
  Conduct committee roles and volunteering at PostgreSQL events, with slides and
  recordings; postgresql.org contributor and release-note recognition; Planet PostgreSQL;
  mailing-list activity from Hackorum; meetups and open source projects.
- **Blog posts**: PostgreSQL posts on percona.com and percona.community.

## How it works

`.github/workflows/update.yml` runs daily at 05:17 UTC, on every push to `main`, or
manually (*Actions → Update PG activity dashboard → Run workflow*). It:

1. runs `collect_community.py` → `community.json` (HTTP responses are cached between runs
   with `actions/cache`, so a warm run only re-fetches current events and feeds),
2. clones PostgreSQL's recent history and runs `build_dashboard_data.py` → `data.json`,
3. runs `percona_activity.py --company-statistics --company-chart` → `chart.svg`, `stats.md`,
4. publishes everything with `index.html` to GitHub Pages.

| File | Purpose |
|---|---|
| `percona_activity.py` | Identity and company attribution for commits (`COMPANY_DOMAINS`, `COMPANY_NAME_OVERRIDES`, `MANUAL_IDENTITIES`); also a standalone CLI report |
| `build_dashboard_data.py` | Builds `data.json`: per-change details, versions, areas, categories, monthly company counts |
| `collect_community.py` | Builds `community.json` from conference sites, blogs, Planet PostgreSQL, Hackorum, postgresql.org |
| `community/people.json` | Percona people to look for at events, with optional `since`/`until` dates |
| `community/manual.json` | Hand-curated meetups, community days, volunteering and projects no website lists |
| `index.html` | The dashboard (plain HTML/JS, no dependencies) |

### Community sources

| Source | What we take |
|---|---|
| PostgreSQL Europe events (postgresql.eu): PGConf.EU, PGConf.DE, PGConf.PL, FOSDEM PGDay, PGDay Paris, Nordic PGDay, PGDay Lowlands, PGDay Napoli, PGDay Med | talks (`schedule.xml`), speaker companies, organisers and committees (`/organisation/`), slides and video links |
| PgUS (postgresql.us), PGDay UK (pgday.uk), PGConf.dev (pgevents.ca) | same, via the same conference system |
| Swiss PGDay (pgday.ch) | talks and committees from its own site |
| percona.com/blog, percona.community | PostgreSQL posts (RSS) |
| Planet PostgreSQL | Percona feeds and recent post count |
| Hackorum (`/team/percona`) | mailing-list messages, patches, per year |
| postgresql.org | contributors page, release-note acknowledgments |

**How a person is counted at an event:** the event lists them with company Percona, or they
are in `community/people.json`, the event is inside their `since`/`until` window, and the
event does not list another employer. Speaker profiles show a person's *current* company,
so they are only used as evidence for events in the last ~18 months. Entries dropped
because a source names another employer are listed under `review` in `community.json`.

### Volunteering

Volunteer schedules on the conference system (`/events/<event>/volunteer/`) need a login,
so the daily build can't read them. Import them locally with your own session instead:

```
python3 import_volunteering.py --session 'www.postgresql.eu=<sessionid>' --session 'postgresql.us=<sessionid>'
python3 import_volunteering.py --session 'www.postgresql.eu=<sessionid>' --write   # merge into community/volunteering.csv
```

`<sessionid>` is the `sessionid` cookie of that site while you're logged in (browser developer
tools → Cookies); each site (postgresql.eu, postgresql.us, pgday.uk, pgevents.ca) has its own.
The script only sees events where you were a volunteer or admin, and lists the Percona
people on those events' volunteer lists. Review the CSV diff, then commit. Volunteering at
events outside this system (FOSDEM, meetups) goes in `community/manual.json`.

New conference series on these systems are picked up automatically. To add a different
kind of site, add a collector function in `collect_community.py`.

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
