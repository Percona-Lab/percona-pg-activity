#!/usr/bin/env python3
"""
Build ranking.json: per-person authored/reviewed/reported counts for the
contributor ranking tab, committers included.

Run from inside a postgres/postgres clone, like build_dashboard_data.py:

    python3 /path/to/build_ranking_data.py --after 2025-12-31 --output site/ranking.json

Changes are grouped by subject across all branches, so a backpatched fix
counts once, the same as on the code tab. The "cycle" section covers the
newest stable branch since it split from the previous one (the PostgreSQL 19
cycle while REL_19_STABLE is the newest), one credit per commit; the clone
must reach back to that split point, otherwise the section is left out. Committers get an author credit
when a trailer names them, or for a change of their own with no Author:
trailer but a Discussion: link; their own changes with neither (typo fixes,
catversion bumps, buildfarm cleanups) are counted as "skip" instead.
"""

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import percona_activity as pa  # noqa: E402
import build_dashboard_data as bd  # noqa: E402

# Per-month counter slots, in output order.
SLOTS = ["a", "r", "rep", "touched", "cm", "self", "skip"]
# Also catches a Discussion: link glued onto another trailer line.
DISCUSSION_RE = re.compile(r"\bDiscussion\s*:\s*<?https?://", re.I)
ROLE_SLOT = {"authored": "a", "reviewed": "r", "reported": "rep", "diagnosed": "rep"}


def committers(args: list[str]) -> dict[str, tuple[str, str]]:
    out = {}
    for line in bd.git("log", *args, "--format=%H%x09%cN%x09%cE").splitlines():
        h, name, email = line.split("\t")
        out[h] = (name, email.lower())
    return out


def rank(raw: str, cby: dict, by_subject: bool) -> dict:
    attr = bd.Attribution(raw)
    groups: dict[str, dict] = {}
    for h, cdate, subject, body_lines in bd.parse_commits(raw):
        g = groups.setdefault(subject if by_subject else h, {
            "date": cdate, "credits": defaultdict(set), "committers": set(), "author": False, "discussion": False})
        g["date"] = min(g["date"], cdate)
        if h in cby:
            g["committers"].add(attr.resolve(*cby[h])[0])
        for line in body_lines:
            if DISCUSSION_RE.search(line):
                g["discussion"] = True
            t = pa.parse_trailer(line)
            if not t:
                continue
            role, people = t
            if role == "authored":
                g["author"] = True
            if role not in ROLE_SLOT:
                continue
            for name, email in people:
                g["credits"][attr.resolve(name, email)[0]].add(ROLE_SLOT[role])

    counts = defaultdict(lambda: defaultdict(lambda: dict.fromkeys(SLOTS, 0)))
    committer_names = set()
    for g in groups.values():
        month = g["date"][:7]
        credits = g["credits"]
        for c in g["committers"]:
            committer_names.add(c)
            counts[c][month]["cm"] += 1
            if g["author"]:
                continue
            if g["discussion"]:
                credits[c].add("a")
                counts[c][month]["self"] += 1
            else:
                counts[c][month]["skip"] += 1
        for person, slots in credits.items():
            m = counts[person][month]
            for s in slots:
                m[s] += 1
            m["touched"] += 1

    companies = {}
    for _, _, _, name, email in attr.mentions:
        display, company = attr.resolve(name, email)
        companies.setdefault(display, company)
    for name in committer_names:
        companies.setdefault(name, attr.resolve(name, None)[1])

    people = []
    for name, months in counts.items():
        if not any(m["a"] or m["r"] for m in months.values()) and name not in committer_names:
            continue
        people.append({
            "name": name,
            "company": companies.get(name),
            "committer": name in committer_names,
            "months": {k: [months[k][s] for s in SLOTS] for k in sorted(months)},
        })
    people.sort(key=lambda p: (-sum(v[0] + v[1] for v in p["months"].values()), p["name"]))
    by_month = defaultdict(int)
    for g in groups.values():
        by_month[g["date"][:7]] += 1
    return {"changes_by_month": dict(sorted(by_month.items())), "people": people}


def cycle() -> dict | None:
    stable = sorted((int(m.group(1)), r) for r in bd.git(
        "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/").split()
        if (m := re.fullmatch(r"origin/REL_(\d+)_STABLE", r)))
    if len(stable) < 2:
        return None
    (_, prev), (version, newest) = stable[-2:]
    try:
        base = bd.git("merge-base", prev, newest).strip()
        bd.git("cat-file", "-e", f"{base}^{{commit}}")
    except subprocess.CalledProcessError:
        print(f"no merge-base of {prev} and {newest} in this clone, skipping the cycle", file=sys.stderr)
        return None
    rng = ["--no-merges", f"{base}..{newest}"]
    raw = bd.git("log", *rng, "--date=short", "--format=COMMIT %H %ad %s%n%B%nENDCOMMIT")
    head = bd.git("log", "-1", "--format=%H%x09%cs", newest).strip().split("\t")
    return {"version": version, "branch": newest, "base": base[:11], "head": head[0][:11],
            "base_date": bd.git("log", "-1", "--format=%cs", base).strip(), "head_date": head[1],
            **rank(raw, committers(rng), by_subject=False)}


def build(after: str, before: str | None) -> dict:
    args = ["--all", f"--after={after}"] + ([f"--before={before}"] if before else [])
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "company": pa.HIGHLIGHT_COMPANY,
        "range": {"after": after, "before": before},
        "slots": SLOTS,
        **rank(pa.fetch_log(after, before), committers(args), by_subject=True),
        "cycle": cycle(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--after", default=pa.DEFAULT_AFTER, metavar="DATE")
    parser.add_argument("--before", default=None, metavar="DATE")
    parser.add_argument("--output", default="ranking.json", metavar="PATH")
    args = parser.parse_args()
    data = build(args.after, args.before)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    cyc = data["cycle"]
    print(f"{len(data['people'])} people, {sum(p['committer'] for p in data['people'])} committers"
          + (f"; PostgreSQL {cyc['version']} cycle: {len(cyc['people'])} people" if cyc else "") + f" -> {out}")


if __name__ == "__main__":
    main()
