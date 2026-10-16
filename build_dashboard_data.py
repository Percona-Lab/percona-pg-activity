#!/usr/bin/env python3
"""
Build data.json for the Percona PostgreSQL contribution dashboard.

Run from inside a postgres/postgres clone (needs commit history, trees for the
date range, and refs/remotes/origin/{master,REL_*_STABLE}):

    python3 /path/to/build_dashboard_data.py --after 2025-12-31 --output site/data.json

Reuses the identity/company attribution from percona_activity.py, so the
company numbers match `percona_activity.py --company-statistics` exactly.
"""

import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import percona_activity as pa  # noqa: E402

COMPANY = pa.HIGHLIGHT_COMPANY
TOP_COMPANIES = 25

HEADER_RE = re.compile(r"^COMMIT ([0-9a-f]{40}) (\d{4}-\d{2}-\d{2}) (.+)$")
DISCUSSION_RE = re.compile(r"^Discussion:\s*(https?://\S+)", re.IGNORECASE)
TRAILER_LINE_RE = re.compile(r"^[A-Z][A-Za-z-]+(-by|-through|-version)?:\s")

# Path prefix -> area of PostgreSQL. First match wins, so specific before general.
AREA_RULES = [
    ("src/backend/optimizer", "Query planner"),
    ("src/backend/executor", "Executor"),
    ("src/backend/parser", "SQL parser"),
    ("src/backend/commands", "SQL commands & DDL"),
    ("src/backend/replication/logical", "Logical replication"),
    ("src/backend/replication", "Replication"),
    ("src/backend/access/transam", "WAL & transactions"),
    ("src/backend/access/heap", "Heap storage"),
    ("src/backend/access/nbtree", "Indexes"),
    ("src/backend/access/gin", "Indexes"),
    ("src/backend/access/gist", "Indexes"),
    ("src/backend/access/brin", "Indexes"),
    ("src/backend/access/hash", "Indexes"),
    ("src/backend/access/spgist", "Indexes"),
    ("src/backend/access/index", "Indexes"),
    ("src/backend/access", "Table & index access"),
    ("src/backend/storage", "Storage & buffers"),
    ("src/backend/postmaster", "Server processes"),
    ("src/backend/utils/activity", "Monitoring & statistics"),
    ("src/backend/statistics", "Planner statistics"),
    ("src/backend/utils/adt", "Data types & functions"),
    ("src/backend/utils/mmgr", "Memory management"),
    ("src/backend/utils/cache", "Catalog caches"),
    ("src/backend/catalog", "System catalog"),
    ("src/backend/libpq", "Authentication & protocol"),
    ("src/interfaces/libpq", "libpq client library"),
    ("src/backend/tcop", "Query processing"),
    ("src/backend/jit", "JIT"),
    ("src/backend/partitioning", "Partitioning"),
    ("src/backend/rewrite", "Rules & views"),
    ("src/backend/nodes", "Query nodes"),
    ("src/bin/pg_dump", "pg_dump / pg_restore"),
    ("src/bin/psql", "psql"),
    ("src/bin/pg_upgrade", "pg_upgrade"),
    ("src/bin/pg_basebackup", "Backup tools"),
    ("src/bin/pg_combinebackup", "Backup tools"),
    ("src/bin/pg_rewind", "pg_rewind"),
    ("src/bin/pgbench", "pgbench"),
    ("src/bin/", "Client & server tools"),
    ("src/pl/", "Procedural languages"),
    ("src/common", "Common & portability"),
    ("src/port", "Common & portability"),
    ("src/backend/port", "Common & portability"),
    ("src/backend/utils/misc", "Configuration & GUCs"),
    ("src/backend/backup", "Backup tools"),
    ("src/fe_utils/astreamer", "Backup tools"),
    ("src/fe_utils", "Client & server tools"),
    ("src/backend/tsearch", "Full-text search"),
    ("src/backend/", "Server internals"),
    ("doc/", "Documentation"),
    ("src/test", "Tests"),
    ("src/tools", "Build & tooling"),
    ("meson", "Build & tooling"),
    ("configure", "Build & tooling"),
    (".", "Build & tooling"),
]
SECONDARY_AREAS = {"Documentation", "Tests", "Build & tooling"}

CATEGORY_RULES = [
    ("Documentation", re.compile(r"^(doc|docs)\b|\bdocumentation\b|\btypo", re.I)),
    ("Testing", re.compile(r"^(test|tests)\b|\b(tap|regression|isolation) tests?\b|\btests? (setup|coverage|infrastructure|suite)\b|^add (a )?tests?\b", re.I)),
    ("Bug fix", re.compile(r"^(fix|correct|repair|prevent|avoid|handle|don'?t|do not|guard|protect)\b|\b(fix|fixes|crash|bug|race|leak|assert(ion)? failure|incorrect|wrong)\b", re.I)),
    ("Performance", re.compile(r"\b(speed ?up|faster|optimi[sz]|performance|vectori[sz]|reduce .*(overhead|cost|contention)|cheaper|simd|avx)\b", re.I)),
    ("New feature", re.compile(r"^(add|support|allow|implement|introduce|teach|enable|new|provide|expose)\b", re.I)),
    ("Refactoring", re.compile(r"^(refactor|clean ?up|cleanup|remove|rename|simplify|move|reorganize|split|use|replace|tidy)\b", re.I)),
]


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True)


def area_for(path: str) -> str:
    if path.startswith("contrib/"):
        return "Extension: " + path.split("/")[1]
    for prefix, area in AREA_RULES:
        if path.startswith(prefix):
            return area
    return "Server internals"


def category_for(subject: str, backpatched: bool) -> str:
    # "pg_dump: Don't emit ..." -- judge the verb after a component prefix,
    # but keep the prefix for the documentation check ("doc: ...").
    verb_part = re.sub(r"^[\w./ -]{1,30}:\s+", "", subject)
    for label, rx in CATEGORY_RULES:
        if rx.search(subject if label == "Documentation" else verb_part):
            return label
    return "Bug fix" if backpatched else "Improvement"


def branch_versions(after: str) -> tuple[dict[str, set[str]], int]:
    """hash -> set of PostgreSQL major versions it ships in, plus the dev version.
    A master commit made before REL_N_STABLE was branched is reachable from
    that branch too, so it maps to N (the release it shipped in)."""
    refs = git("for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/").split()
    stable = {}
    for ref in refs:
        m = re.fullmatch(r"origin/REL_(\d+)_STABLE", ref)
        if m:
            stable[int(m.group(1))] = ref
    dev_version = max(stable) + 1 if stable else 0
    versions: dict[str, set[str]] = defaultdict(set)
    for ver, ref in stable.items():
        for h in git("rev-list", f"--after={after}", ref).split():
            versions[h].add(str(ver))
    if "origin/master" in refs:
        for h in git("rev-list", f"--after={after}", "origin/master").split():
            if h not in versions:
                versions[h].add(str(dev_version))
    return versions, dev_version


def release_status(versions: set[int]) -> dict[str, str]:
    """Major version -> "released", or its latest pre-release tag (e.g. "RC1"),
    or "in development", from the tags on the upstream remote."""
    try:
        out = git("ls-remote", "--tags", "--refs", "origin", "REL_*")
    except subprocess.CalledProcessError:
        out = ""
    tags = {line.split("refs/tags/")[1] for line in out.splitlines() if "refs/tags/" in line}
    status = {}
    for v in sorted(versions):
        if f"REL_{v}_0" in tags:
            status[str(v)] = "released"
            continue
        pre = sorted((t[len(f"REL_{v}_"):] for t in tags
                      if re.fullmatch(rf"REL_{v}_(BETA|RC)\d+", t)),
                     key=lambda t: (t.startswith("RC"), int(re.sub(r"\D", "", t))))
        status[str(v)] = pre[-1] if pre else "in development"
    return status


def changed_files(hashes: list[str]) -> dict[str, list[str]]:
    if not hashes:
        return {}
    out = git("log", "--no-walk=unsorted", "--name-only", "--format=@@%H", *hashes)
    files: dict[str, list[str]] = {}
    current = None
    for line in out.splitlines():
        if line.startswith("@@"):
            current = line[2:]
            files[current] = []
        elif line.strip() and current:
            files[current].append(line.strip())
    return files


def summarize_body(lines: list[str]) -> str:
    """The commit message body, minus trailers, as plain paragraphs."""
    body = []
    for line in lines:
        if TRAILER_LINE_RE.match(line) or DISCUSSION_RE.match(line):
            continue
        body.append(line.rstrip())
    text = "\n".join(body).strip()
    paragraphs = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text)]
    return "\n\n".join(p for p in paragraphs if p)


class Attribution:
    """Resolve any trailer name/email to (display name, company)."""

    def __init__(self, raw: str):
        self.mentions = pa.collect_all_mentions(raw)
        self.idx = pa.IdentityIndex(self.mentions)
        self.known_names = {("name", pa._norm_name(n)) for _, _, _, n, _ in self.mentions if n}
        self.known_emails = {("email", e) for _, _, _, _, e in self.mentions if e}

    def resolve(self, name: str, email: str | None, when: str | None = None) -> tuple[str, str | None]:
        """Display name and company on date *when* (default: today)."""
        nkey = ("name", pa._norm_name(name)) if name else None
        ekey = ("email", email) if email else None
        if nkey in self.known_names or (not nkey and ekey in self.known_emails):
            root = self.idx.root(name, email)
            return self.idx.display_name(root), pa.company_for(self.idx, root, when)
        if ekey in self.known_emails:
            root = self.idx.root(None, email)
            return name or self.idx.display_name(root), pa.company_for(self.idx, root, when)
        company = (pa.history_company(pa._norm_name(name), when)
                   or pa.COMPANY_NAME_OVERRIDES.get(pa._norm_name(name))) if name else None
        if not company and email:
            company = pa.COMPANY_DOMAINS.get(email.split("@")[-1])
        return name or email, company


def parse_commits(raw: str):
    for block in re.split(r"\nENDCOMMIT\n", raw):
        lines = block.strip().splitlines()
        if not lines:
            continue
        m = HEADER_RE.match(lines[0])
        if not m:
            continue
        # lines[1] repeats the subject (%B), so the body starts after it.
        yield m.group(1), m.group(2), m.group(3).strip(), lines[2:]


def build(after: str, before: str | None) -> dict:
    raw = pa.fetch_log(after, before)
    attr = Attribution(raw)

    # ── Company ranking, bucketed by month so the page can filter ranges ──
    first_seen: dict = defaultdict(dict)
    for role, cdate, subject, name, email in attr.mentions:
        root = attr.idx.root(name, email)
        if root is None:
            continue
        bucket = "reviewed" if "review" in role else "authored"
        cur = first_seen[root].get((bucket, subject))
        if cur is None or cdate < cur:
            first_seen[root][(bucket, subject)] = cdate
    company_monthly = {"authored": defaultdict(Counter), "reviewed": defaultdict(Counter)}
    for root, keys in first_seen.items():
        for (bucket, _), cdate in keys.items():
            company = pa.company_for(attr.idx, root, cdate) or "Unattributed"
            company_monthly[bucket][company][cdate[:7]] += 1
    totals = Counter()
    for bucket in company_monthly.values():
        for company, months in bucket.items():
            if company != "Unattributed":
                totals[company] += sum(months.values())
    keep = {c for c, _ in totals.most_common(TOP_COMPANIES)} | {COMPANY, "Unattributed"}
    companies = {
        bucket: {c: dict(sorted(m.items())) for c, m in data.items() if c in keep}
        for bucket, data in company_monthly.items()
    }
    other_monthly = {
        bucket: dict(sorted(sum((m for c, m in data.items() if c not in keep), Counter()).items()))
        for bucket, data in company_monthly.items()
    }
    company_count = len([c for c in totals if totals[c] > 0])

    # ── All commits grouped by subject (backports collapse into one change) ──
    groups: dict[str, dict] = {}
    total_by_month = Counter()
    for h, cdate, subject, body_lines in parse_commits(raw):
        g = groups.get(subject)
        if g is None:
            g = groups[subject] = {
                "subject": subject, "date": cdate, "hashes": [], "body": body_lines,
                "credits": defaultdict(set), "people": {}, "discussion": [],
            }
        g["hashes"].append(h)
        if cdate < g["date"]:
            g["date"] = cdate
        for line in body_lines:
            dm = DISCUSSION_RE.match(line.strip())
            if dm and dm.group(1) not in g["discussion"]:
                g["discussion"].append(dm.group(1))
            t = pa.parse_trailer(line)
            if not t:
                continue
            role, people = t
            for name, email in people:
                display, company = attr.resolve(name, email, cdate)
                g["credits"][display].add(role)
                g["people"][display] = company
    for g in groups.values():
        total_by_month[g["date"][:7]] += 1

    percona_groups = [
        g for g in groups.values()
        if any(c == COMPANY for c in g["people"].values())
    ]

    versions, dev_version = branch_versions(after)
    # Prefer the master commit as the canonical one (it carries the full message).
    master_hashes = set(git("rev-list", f"--after={after}", "origin/master").split()) \
        if "origin/master" in git("for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/").split() else set()
    for g in percona_groups:
        g["hashes"].sort(key=lambda h: (h not in master_hashes,))
    files = changed_files([g["hashes"][0] for g in percona_groups])

    commits = []
    for g in sorted(percona_groups, key=lambda g: g["date"], reverse=True):
        vers = sorted({v for h in g["hashes"] for v in versions.get(h, ())}, key=int)
        released = [v for v in vers if int(v) < dev_version]
        backpatched = len(released) > 1 or (len(vers) > 1)
        areas = Counter(area_for(p) for p in files.get(g["hashes"][0], [])
                        if not p.startswith("src/include/"))
        primary = [a for a, _ in areas.most_common() if a not in SECONDARY_AREAS]
        area_list = (primary or [a for a, _ in areas.most_common()])[:2]
        credits = [
            {"name": person, "company": g["people"][person], "roles": sorted(roles)}
            for person, roles in g["credits"].items()
        ]
        commits.append({
            "hash": g["hashes"][0],
            "date": g["date"],
            "subject": g["subject"],
            "summary": summarize_body(g["body"]),
            "category": category_for(g["subject"], backpatched),
            "areas": area_list,
            "versions": vers,
            "backpatch_count": len(g["hashes"]) - 1,
            "discussion": g["discussion"],
            "credits": credits,
        })

    people = defaultdict(lambda: Counter())
    for c in commits:
        for cr in c["credits"]:
            if cr["company"] == COMPANY:
                for role in cr["roles"]:
                    people[cr["name"]][role] += 1

    head = git("log", "-1", "--format=%H%x09%cs%x09%s", "origin/master").strip().split("\t") \
        if master_hashes else ["", "", ""]
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "company": COMPANY,
        "range": {"after": after, "before": before},
        "postgres_head": {"hash": head[0][:11], "date": head[1], "subject": head[2]},
        "dev_version": dev_version,
        "release_status": release_status({int(v) for c in commits for v in c["versions"]}),
        "total_changes_by_month": dict(sorted(total_by_month.items())),
        "company_count": company_count,
        "companies": companies,
        "companies_other": other_monthly,
        "people": {p: dict(c) for p, c in sorted(people.items())},
        "commits": commits,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--after", default=pa.DEFAULT_AFTER, metavar="DATE")
    parser.add_argument("--before", default=None, metavar="DATE")
    parser.add_argument("--output", default="data.json", metavar="PATH")
    args = parser.parse_args()
    data = build(args.after, args.before)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    print(f"{len(data['commits'])} {COMPANY} changes, {len(data['people'])} people -> {out}")


if __name__ == "__main__":
    main()
