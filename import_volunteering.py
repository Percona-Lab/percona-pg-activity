#!/usr/bin/env python3
"""
Import Percona volunteering from pgeu-system volunteer schedules.

Volunteer schedules (https://www.postgresql.eu/events/<event>/volunteer/) are
only visible when logged in, to that event's volunteers and admins, so the
daily build cannot read them. Run this locally with your own session instead:

    python3 import_volunteering.py \\
        --session 'www.postgresql.eu=<sessionid>' \\
        --session 'postgresql.us=<sessionid>'

(Quote each value; pasting the whole cookie string "csrftoken=...; sessionid=..."
also works, the session id is picked out of it.)

Get each <sessionid> from your browser while logged in to that site
(developer tools > Storage/Application > Cookies > "sessionid"). Each site
has its own login and cookie. The cookie is only sent to the site it
belongs to and is never stored.

For every event on those sites it asks the schedule API whether you can see
the volunteer list (404 = you were neither volunteer nor admin there), and
collects the Percona people (community/people.json) on it. It prints what it
found; --write merges it into community/volunteering.csv for you to review
and commit.
"""

import argparse
import csv
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect_community as cc  # noqa: E402

CSV_PATH = cc.ROOT / "community" / "volunteering.csv"
FIELDS = ["person", "event", "date", "slots"]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch_schedule(url: str, session: str) -> tuple[int, dict | None]:
    req = urllib.request.Request(url, headers={"User-Agent": cc.USER_AGENT, "Cookie": f"sessionid={session}",
                                               "Accept": "application/json"})
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, None
    except ValueError:
        return 200, None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--session", action="append", default=[], metavar="HOST=SESSIONID",
                        help="session cookie per site, e.g. www.postgresql.eu=abc123 (repeatable)")
    parser.add_argument("--write", action="store_true", help=f"merge results into {CSV_PATH.relative_to(cc.ROOT)}")
    parser.add_argument("--cache", default=str(cc.ROOT / ".cache" / "http"), metavar="DIR")
    args = parser.parse_args()

    # Accept a bare session id or a whole pasted cookie string ("csrftoken=..; sessionid=..").
    sessions = {}
    for item in args.session:
        host, _, value = item.partition("=")
        m = cc.re.search(r"(?:^|;\s*)sessionid=([^;\s]+)", value)
        sessions[host.strip()] = m.group(1) if m else value.strip()
    if not sessions:
        parser.error("give at least one --session HOST=SESSIONID")
    http = cc.Http(Path(args.cache))
    roster = cc.Roster(cc.ROOT / "community" / "people.json")
    found = []

    for inst in cc.PGEU_INSTANCES:
        host = urlparse(inst["base"]).netloc
        session = sessions.get(host) or sessions.get(host.removeprefix("www."))
        if not session:
            continue
        print(f"{host}:")
        for slug, site, year, _ in cc.pgeu_events(http, inst):
            if year > cc.date.today().year:
                continue
            status, data = fetch_schedule(f"{inst['base']}/events/{slug}/volunteer/api/", session)
            if status in (301, 302, 303, 401, 403):
                print(f"  ! not logged in on {host} (HTTP {status}) - check the session cookie")
                break
            if status != 200 or not data:
                continue      # 404: you were neither volunteer nor admin for this event
            conf = {"id": slug}
            cc.parse_frab(http.get(f"{inst['base']}/events/{slug}/schedule.xml", max_age_days=365), conf)
            when = conf.get("start")
            slots = {}
            for slot in data.get("slots", []):
                for a in slot.get("assignments", []):
                    slots[a.get("volunteer")] = slots.get(a.get("volunteer"), 0) + 1
            hits = []
            for vol in data.get("volunteers", []):
                hit = roster.match(vol.get("name", ""), None, when, f"{slug} volunteer")
                if hit:
                    hits.append({"person": hit[0], "event": slug, "date": when or "", "slots": slots.get(vol["name"], 0)})
            print(f"  {slug:<22} {len(data.get('volunteers', [])):>3} volunteers, Percona: "
                  + (", ".join(f"{h['person']} ({h['slots']} slots)" for h in hits) or "-"))
            found += hits

    if not args.write:
        print(f"\n{len(found)} entries. Re-run with --write to merge them into {CSV_PATH.relative_to(cc.ROOT)}.")
        return
    rows = {}
    if CSV_PATH.exists():
        with CSV_PATH.open(newline="") as f:
            rows = {(r["person"], r["event"]): r for r in csv.DictReader(f)}
    for h in found:
        rows[(h["person"], h["event"])] = {k: str(h[k]) for k in FIELDS}
    with CSV_PATH.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for key in sorted(rows, key=lambda k: (rows[k]["date"], k)):
            w.writerow(rows[key])
    print(f"\n{len(rows)} entries in {CSV_PATH.relative_to(cc.ROOT)}. Review the diff, then commit and push.")


if __name__ == "__main__":
    main()
