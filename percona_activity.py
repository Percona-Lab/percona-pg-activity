#!/usr/bin/env python3
"""
Report authored and reviewed patch counts per contributor.
Deduplicates backports by matching on commit subject line.
Auto-discovers every @percona.com address from the git log so no one is missed.

Usage:
    python3 percona_activity.py
    python3 percona_activity.py --after 2025-09-01
    python3 percona_activity.py --after 2025-12-01 --before 2026-03-01
    python3 percona_activity.py --graph
    python3 percona_activity.py --company-statistics
    python3 percona_activity.py --company-statistics --after 2025-09-22 --top-n 10
    python3 percona_activity.py --company-statistics --after 2025-09-22 --list-unattributed --top-n 30
    python3 percona_activity.py --company-statistics --after 2025-09-22 --company-chart
"""

import argparse
import re
import subprocess
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

# ── Manually tracked contributors ────────────────────────────────────────────
# Add anyone whose @percona.com email won't appear in the log, or external
# contributors you want to track alongside Percona staff.
# Each entry: "Display Name" -> list of lowercase email/name fragments to match.
MANUAL_IDENTITIES: dict[str, list[str]] = {
    "Andreas Karlsson": ["andreas@proxel.se", "andreas karlsson"],
}

# ── Date range defaults ───────────────────────────────────────────────────────
DEFAULT_AFTER  = "2025-11-30"   # inclusive of Dec 2025 onwards
DEFAULT_BEFORE = None           # None = up to HEAD
DEFAULT_GRAPH_OUTPUT = "percona_activity_timeline.svg"
DEFAULT_COMPANY_TOP_N = 20
DEFAULT_COMPANY_CHART_OUTPUT = "percona_company_chart.svg"
HIGHLIGHT_COMPANY = "Percona"

# ── Company attribution for --company-statistics ────────────────────────────
# Corporate email domains that map straight to a company.
COMPANY_DOMAINS: dict[str, str] = {
    "enterprisedb.com": "EDB",
    "fujitsu.com": "Fujitsu",
    "postgrespro.ru": "Postgres Professional",
    "postgrespro.com": "Postgres Professional",
    "highgo.com": "Highgo Software",
    "highgo.ca": "Highgo Software",
    "tigerdata.com": "Tiger Data (Timescale)",
    "timescale.com": "Tiger Data (Timescale)",
    "cybertec.at": "Cybertec",
    "dalibo.com": "Dalibo",
    "google.com": "Google",
    "amazon.com": "Amazon",
    "sraoss.co.jp": "SRA OSS",
    "datadoghq.com": "Datadog",
    "ibm.com": "IBM",
    "intel.com": "Intel",
    "aiven.io": "Aiven",
    "zohocorp.com": "Zoho",
    "alibaba-inc.com": "Alibaba",
    "starlingbank.com": "Starling Bank",
    "servicenow.com": "ServiceNow",
    "linaro.org": "Linaro",
    "hpe.com": "HPE",
    "oss.nttdata.com": "NTT DATA",
    "nttdata.com": "NTT DATA",
    "tantorlabs.com": "Tantor Labs",
    "percona.com": "Percona",
    "halodbtech.com": "HaloDB",
    "uni-muenster.de": "University of Munster",
    "apple.com": "Apple",
    "planetscale.com": "PlanetScale",
    "turacolabs.com": "Turaco Labs",
    "postgres.ai": "Postgres.ai",
    "pganalyze.com": "pganalyze",
}

# People who post from a personal-looking address but whose employer is known
# from public bios (LinkedIn, project announcements, conference speaker
# pages). Researched manually -- update as people change jobs. Keyed by
# lowercased, accent-stripped display name.
COMPANY_NAME_OVERRIDES: dict[str, str] = {
    "tom lane": "Snowflake",
    "chao li": "Highgo Software",
    "fujii masao": "NTT DATA",
    "melanie plageman": "Microsoft",
    "michael paquier": "Amazon",
    "bertrand drouvot": "Amazon",
    "richard guo": "OpenPie",
    "daniel gustafsson": "Microsoft",
    "david rowley": "Microsoft",
    "jian he": "EDB",
    "xuneng zhou": "Highgo Software",
    "alvaro herrera": "EDB",
    "jelte fennema-nio": "MotherDuck",
    "sami imseih": "Amazon",
    "ashutosh bapat": "EDB",
    "corey huinker": "Apple",
    "satyanarayana narlapuram": "Microsoft",
    "andres freund": "Microsoft",
    "amit kapila": "Fujitsu",
    "heikki linnakangas": "Databricks",
    "masahiko sawada": "Amazon",
    "zsolt parragi": "Percona",
    "nazir bilal yavuz": "Microsoft",
    "nathan bossart": "Amazon",
    "robert haas": "EDB",
    "peter geoghegan": "Amazon",
    "thomas munro": "Microsoft",
    "alexander korotkov": "Supabase",
    "tomas vondra": "Microsoft",
    "andrew dunstan": "EDB",
    "david geier": "Tiger Data (Timescale)",
    "baji shaik": "Amazon",
    "shinya kato": "NTT DATA",
    "andreas karlsson": "Percona",
    "shveta malik": "Fujitsu",
    "vignesh c": "Fujitsu",
    "lukas fittl": "pganalyze",
    "matheus alcantara": "EDB",
    "matthias van de meent": "Databricks",
    "andrey borodin": "Yandex",
    "paul a jungwirth": "Illuminated Computing",
    "euler taveira": "EDB",
    "amit langote": "Microsoft",
    "tristan partin": "Amazon",
    "shlok kyal": "Fujitsu",
    "dilip kumar": "Google",
    "noah misch": "DCX - Multi Axis Games",
    "peter eisentraut": "EDB",
    "john naylor": "Amazon",
    # Researched 2026-08-21 via GitHub company field / LinkedIn / postgresql.org
    # contributor-profile page / company blogs / mailing-list signatures, to
    # attribute the "unattributed" long tail from --company-statistics.
    "kirill reshke": "Yandex",
    "junwang zhao": "Ant Group",
    "tender wang": "Tencent",
    "srinath reddy sadipiralla": "EDB",
    "bharath rupireddy": "Amazon",
    "etsuro fujita": "NTT DATA",
    "greg burd": "Amazon",
    "robert treat": "Amazon",
    "amul sul": "EDB",
    "kyotaro horiguchi": "NTT DATA",
    "andrey rachitskiy": "Postgres Professional",
    "haibo yan": "Apple",
    "nisha moond": "Fujitsu",
    "solai v": "C-DAC",
    "vasuki m": "C-DAC",
    "mahendra singh thalor": "EDB",
    "bryan green": "EDB",
    "greg sabino mullane": "Crunchy Data",
    "jonathan gonzalez v": "EDB",
    "thom brown": "Data Egret",
    "tatsuo ishii": "SRA OSS",
    "xiaopeng wang": "Microsoft",
    "joel jacobson": "Glue Finance AB",
    "ian lawrence barwick": "EDB",
    "christoph berg": "Cybertec",
    "jeff davis": "Databricks",
    "michael banck": "NetApp",
    "andrei lepikhov": "pgEdge",
    "shihao zhong": "Google",
    "huseyin demir": "Adyen",
    "pavel borisov": "Supabase",
    "mark atwood": "Review Commit LLC",
    "rafia sabih": "Cybertec",
    "maciek sakrejda": "pganalyze",
    "david e wheeler": "ClickHouse",
    "nitin jadhav": "Microsoft",
    "imran zaheer": "Cybertec",
    "nikolay samokhvalov": "Postgres.ai",
    "mario gonzalez": "EDB",
    "fabrizio de royes mello": "Tiger Data (Timescale)",
    "karina litskevich": "Postgres Professional",
    "nikhil sontakke": "PlanetScale",
    "niall newman": "Turaco Labs",
    "ajin cherian": "Fujitsu",
    "joonghyuk shin": "Tmax Tibero",
    "kenny chen": "Dcard",
    "tatsuro yamada": "NTT DATA",
    "mohamed ali": "Amazon",
    "dmitry dolgov": "Red Hat",
    "joe conway": "Amazon",
    "rahila syed": "Microsoft",
    "ayush tiwari": "Microsoft",
}


# Spellings of one person that share no email address, so the identity merge
# cannot join them. Keys and values are _norm_name() forms; the value is the
# canonical key, which COMPANY_NAME_OVERRIDES must use. Same-surname people
# (Chao Li / Japin Li / Haiyang Li) are different people.
NAME_ALIASES: dict[str, str] = {
    "masao fujii": "fujii masao",
    "sawada masahiko": "masahiko sawada",
    "wang yuelin": "yuelin wang",
    "hou zhijie": "zhijie hou",
    "paul jungwirth": "paul a jungwirth",
    "srinath reddy": "srinath reddy sadipiralla",
    "mikhail nikalayeu": "mihail nikalayeu",
    "satya narlapuram": "satyanarayana narlapuram",
    "kenny": "kenny chen",
    "bilal yavuz": "nazir bilal yavuz",
    "andrey m borodin": "andrey borodin",
    "burd greg": "greg burd",
    "alvaro": "alvaro herrera",
    "kohei kaigai": "kaigai kohei",
    "albe laurenz": "laurenz albe",
    "tsunakawa takayuki": "takayuki tsunakawa",
    "matsumura ryo": "ryo matsumura",
    "kartyshov ivan": "ivan kartyshov",
    "katsuragi yuta": "yuta katsuragi",
    "li japin": "japin li",
    "tang haiying": "haiying tang",
    "seino yuki": "yuki seino",
    "osumi takamichi": "takamichi osumi",
    "kuroda hayato": "hayato kuroda",
    "zhao junwang": "junwang zhao",
    "zhang mingli": "mingli zhang",
    "zhong shihao": "shihao zhong",
    "ilyasov ian": "ian ilyasov",
    "melnikov maksim": "maksim melnikov",
    "chauhan dhruv": "dhruv chauhan",
    "joao detomini": "joao marcelo detomini",
    "team xint code": "xint code",
    "ian barwick": "ian lawrence barwick",
    "x-man": "zizhuanliu x-man",
    "gregburd": "greg burd",
}

def fetch_log(after: str, before: str | None) -> str:
    cmd = ["git", "log", "--all",
           f"--after={after}",
           "--date=short",
           "--format=COMMIT %H %ad %s%n%B%nENDCOMMIT"]
    if before:
        cmd.append(f"--before={before}")
    return subprocess.check_output(cmd, text=True)


def discover_percona_identities(raw: str) -> dict[str, list[str]]:
    """Scan every trailer line in *raw* and return an IDENTITIES-style dict
    for every unique @percona.com address found."""
    trailer_re = re.compile(
        r"^(?:Author|Co-authored-by|Reviewed-by):\s*(.*?)\s*<(.+?)>",
        re.IGNORECASE,
    )
    # email -> {display_name: occurrence_count}
    email_names: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for line in raw.splitlines():
        m = trailer_re.match(line)
        if not m:
            continue
        name  = m.group(1).strip()
        email = m.group(2).strip().lower()
        if "@percona.com" in email:
            email_names[email][name] += 1

    identities: dict[str, list[str]] = {}
    for email, name_counts in sorted(email_names.items()):
        # Use the most-frequently-seen capitalisation as the canonical display name.
        display_name = max(name_counts, key=name_counts.__getitem__)
        identities[display_name] = [email, display_name.lower()]

    return identities


def build_identities(raw: str) -> dict[str, list[str]]:
    """Merge auto-discovered @percona.com identities with MANUAL_IDENTITIES.
    When the same display name appears in both, their needle lists are combined
    so a manual entry can add extra emails without losing the discovered one."""
    discovered = discover_percona_identities(raw)
    merged = dict(discovered)
    for name, needles in MANUAL_IDENTITIES.items():
        if name in merged:
            merged[name] = sorted(set(merged[name]) | set(needles))
        else:
            merged[name] = needles
    return dict(sorted(merged.items()))


def parse_log(raw: str, identities: dict[str, list[str]]) -> tuple:
    header_re  = re.compile(r"^COMMIT ([0-9a-f]{40}) (\d{4}-\d{2}-\d{2}) (.+)$")
    trailer_re = re.compile(
        r"^(Author|Co-authored-by|Reviewed-by):\s*(.*?)\s*<(.+?)>",
        re.IGNORECASE,
    )

    authored       = {p: set() for p in identities}
    reviewed       = {p: set() for p in identities}
    authored_dates = {p: {} for p in identities}
    reviewed_dates = {p: {} for p in identities}

    for block in re.split(r"\nENDCOMMIT\n", raw):
        lines = block.strip().splitlines()
        if not lines:
            continue
        m = header_re.match(lines[0])
        if not m:
            continue
        commit_date = datetime.strptime(m.group(2), "%Y-%m-%d").date()
        subject = m.group(3).strip()

        for line in lines[1:]:
            tm = trailer_re.match(line)
            if not tm:
                continue
            role      = tm.group(1).lower()
            name_str  = tm.group(2).lower()
            email_str = tm.group(3).lower()

            for person, needles in identities.items():
                if any(n in email_str or n in name_str for n in needles):
                    if "review" in role:
                        reviewed[person].add(subject)
                        current = reviewed_dates[person].get(subject)
                        if current is None or commit_date < current:
                            reviewed_dates[person][subject] = commit_date
                    else:
                        authored[person].add(subject)
                        current = authored_dates[person].get(subject)
                        if current is None or commit_date < current:
                            authored_dates[person][subject] = commit_date

    return authored, reviewed, authored_dates, reviewed_dates


# ── --company-statistics: group EVERY contributor (not just Percona) by
# company, using COMPANY_DOMAINS / COMPANY_NAME_OVERRIDES above ────────────

def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _norm_name(name: str) -> str:
    name = re.sub(r"\(.*?\)", "", name)
    name = _strip_accents(name).lower().replace(".", " ").replace("'", "")
    name = re.sub(r"\s+", " ", name).strip()
    return NAME_ALIASES.get(name, name)


def _split_trailer_names(rest: str) -> list[str]:
    """Split a trailer's value on commas that are NOT inside parentheses, so
    'Name1 (mysterious, offlist), Name2 <e2>' splits into two parts, not three."""
    parts, buf, depth = [], [], 0
    for ch in rest:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _clean_name(name: str) -> str:
    name = re.sub(r"\(.*?\)", "", name)   # balanced parenthetical: "(Fujitsu)"
    name = re.sub(r"\(.*$", "", name)     # unbalanced trailing: "(offlist"
    name = re.sub(r"^[:\s]+", "", name)
    name = re.sub(r"^.*\breported by\s+", "", name, flags=re.I)
    name = re.sub(r"\s+as part of .*$", "", name, flags=re.I)
    name = re.sub(r"^(?:co-)?(?:author|reviewed-by|reported-by)\s*:\s*", "", name, flags=re.I)
    name = re.sub(r"^(?:idea|patch|report|suggestion|based on .*?)\s+(?:from|by)\s+", "", name, flags=re.I)
    name = re.sub(r"\s+", " ", name)
    return name.strip().strip(",;").strip()


_NOISE_NAMES = {"todo", "offlist", "off-list", "off list", ""}

# Trailer key -> role. Keys match case-insensitively; the odd spellings all
# occur in upstream history.
TRAILER_ROLES = {
    "author": "authored",
    "co-authored-by": "authored",
    "co-authored-bu": "authored",
    "co-author": "authored",
    "reviewed-by": "reviewed",
    "reviewed by": "reviewed",
    "reviewer": "reviewed",
    "reported-by": "reported",
    "reported by": "reported",
    "tested-by": "tested",
    "suggested-by": "suggested",
    "suggested by": "suggested",
    "diagnosed-by": "diagnosed",
    "analyzed-by": "diagnosed",
}
_TRAILER_RE = re.compile(r"^([A-Za-z][A-Za-z -]{2,20}?)\s*:\s*(.*)$")
_GLUED_RE = re.compile(r"\s+(?:Discussion|Backpatch[a-z-]*|Author|Reviewed-by|Reported-by)\s*:", re.I)
_EMAIL_RE = re.compile(r"<([^<>]*)>?|([^\s<>]+@[\w.-]+\w)")


def _flip_quoted(m: re.Match) -> str:
    # "Last, First (Dept)" -> First Last
    q = re.sub(r"\(.*?\)", "", m.group(1)).strip()
    last, sep, first = q.partition(",")
    return f"{first.strip()} {last.strip()}" if sep else q


def parse_people(value: str) -> list[tuple[str, str | None]]:
    """Split a trailer value into (name, email_or_None) pairs."""
    value = _GLUED_RE.split(value.strip())[0]
    m = re.match(r"^([^\s,]+),\s*([^\s,]+)\s*(<[^>]*>)$", value)   # Last, First <addr>
    if m:
        value = f"{m.group(2)} {m.group(1)} {m.group(3)}"
    value = re.sub(r'"([^"]+)"', _flip_quoted, value)
    people = []
    for part in _split_trailer_names(value):
        for sub in re.split(r"\s+and\s+", part):
            em = _EMAIL_RE.search(sub)
            email = (em.group(1) or em.group(2) or "").strip().lower() if em else ""
            name = _clean_name(_EMAIL_RE.sub(" ", sub))
            if "@" not in email:
                email = None
            if not name and not email:
                continue
            if not email and (_norm_name(name) in _NOISE_NAMES or len(name) < 3):
                continue
            if len(name) > 60 or name.lower().startswith(("http", "www.")):
                continue
            people.append((name, email))
    return people


def parse_trailer(line: str) -> tuple[str, list[tuple[str, str | None]]] | None:
    """(role, people) for a credit trailer line, else None."""
    m = _TRAILER_RE.match(line.strip())
    if not m:
        return None
    role = TRAILER_ROLES.get(m.group(1).strip().lower())
    if not role:
        return None
    return role, parse_people(m.group(2))


def collect_all_mentions(raw: str) -> list[tuple[str, str, str, str, str | None]]:
    """Parse every authored/reviewed credit trailer for EVERY contributor
    (not just Percona). Handles both 'Name <email>' and the very common
    no-email comma-separated form 'Reviewed-by: Name1, Name2, Name3' --
    roughly a third of all Reviewed-by trailers in this repo have no email at
    all, and a plain email-only regex silently drops them.

    Returns (role, commit_date, subject, name, email_or_None) tuples.
    """
    header_re = re.compile(r"^COMMIT ([0-9a-f]{40}) (\d{4}-\d{2}-\d{2}) (.+)$")

    mentions = []
    for block in re.split(r"\nENDCOMMIT\n", raw):
        lines = block.strip().splitlines()
        if not lines:
            continue
        m = header_re.match(lines[0])
        if not m:
            continue
        commit_date = m.group(2)
        subject = m.group(3).strip()
        for line in lines[1:]:
            t = parse_trailer(line)
            if not t or t[0] not in ("authored", "reviewed"):
                continue
            for name, email in t[1]:
                mentions.append((t[0], commit_date, subject, name, email))
    return mentions


class IdentityIndex:
    """Union-find over contributor names/emails: a name and an email merge
    whenever they co-occur in the same trailer mention, so someone who uses
    several addresses (or gets bare-name-only credit elsewhere) resolves to
    one identity."""

    def __init__(self, mentions):
        self._parent = {}
        self._name_counts = defaultdict(lambda: defaultdict(int))
        for _, _, _, name, email in mentions:
            nkey = ("name", _norm_name(name)) if name else None
            ekey = ("email", email) if email else None
            if nkey:
                self._find(nkey)
            if ekey:
                self._find(ekey)
            if nkey and ekey:
                self._union(nkey, ekey)
        for _, _, _, name, email in mentions:
            if name:
                self._name_counts[self._find(("name", _norm_name(name)))][name] += 1

    def _find(self, x):
        self._parent.setdefault(x, x)
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def _union(self, a, b):
        ra, rb = self._find(a), self._find(b)
        if ra != rb:
            self._parent[ra] = rb

    def root(self, name, email):
        if name:
            return self._find(("name", _norm_name(name)))
        if email:
            return self._find(("email", email))
        return None

    def display_name(self, root):
        counts = self._name_counts.get(root)
        if counts:
            return max(counts, key=counts.__getitem__)
        return root[1] if root[0] == "email" else str(root)

    def emails(self, root):
        return {k for kind, k in self._parent if kind == "email" and self._find((kind, k)) == root}


# People who changed employer: (first day at the new company, company), in any
# order. Credits dated before the earliest entry fall back to
# COMPANY_NAME_OVERRIDES / the email domain, so earlier work stays with the
# previous employer.
COMPANY_NAME_HISTORY: dict[str, list[tuple[str, str]]] = {
    "robert haas": [("2026-09-01", "Databricks")],   # EDB from 2010-04 until 2026-08
}


def history_company(name_key: str, when: str | None) -> str | None:
    """Employer from COMPANY_NAME_HISTORY on date *when* (YYYY-MM-DD, default today)."""
    day = when or date.today().isoformat()
    for start, company in sorted(COMPANY_NAME_HISTORY.get(name_key, ()), reverse=True):
        if day >= start:
            return company
    return None


def company_for(idx: "IdentityIndex", root, when: str | None = None) -> str | None:
    """Company of an identity on date *when* (default: today)."""
    name_key = _norm_name(idx.display_name(root))
    dated = history_company(name_key, when)
    if dated:
        return dated
    if name_key in COMPANY_NAME_OVERRIDES:
        return COMPANY_NAME_OVERRIDES[name_key]
    for email in idx.emails(root):
        domain = email.split("@")[-1]
        if domain in COMPANY_DOMAINS:
            return COMPANY_DOMAINS[domain]
    return None


def compute_company_stats(mentions, idx: "IdentityIndex"):
    """Dedup by (bucket, subject) per identity root -- same backport rule as
    the rest of this script -- then roll each identity up to a company."""
    first_seen: dict = defaultdict(dict)
    for role, cdate, subject, name, email in mentions:
        root = idx.root(name, email)
        if root is None:
            continue
        bucket = "reviewed" if "review" in role else "authored"
        key = (bucket, subject)
        cur = first_seen[root].get(key)
        if cur is None or cdate < cur:
            first_seen[root][key] = cdate

    authored: dict = defaultdict(int)
    reviewed: dict = defaultdict(int)
    unattributed: dict = defaultdict(lambda: {"authored": 0, "reviewed": 0})

    for root, subj_dates in first_seen.items():
        for (bucket, subject), cdate in subj_dates.items():
            company = company_for(idx, root, cdate)
            if company:
                (authored if bucket == "authored" else reviewed)[company] += 1
            else:
                unattributed[root][bucket] += 1

    unattributed_authored = sum(v["authored"] for v in unattributed.values())
    unattributed_reviewed = sum(v["reviewed"] for v in unattributed.values())

    return authored, reviewed, unattributed_authored, unattributed_reviewed, unattributed


def print_unattributed(idx: "IdentityIndex", unattributed: dict, top_n: int) -> None:
    """List contributors with no company match, ranked by total credits, so
    it's clear who to research next and add to COMPANY_NAME_OVERRIDES /
    COMPANY_DOMAINS."""
    rows = []
    for root, counts in unattributed.items():
        total = counts["authored"] + counts["reviewed"]
        emails = sorted(idx.emails(root))
        rows.append((idx.display_name(root), counts["authored"], counts["reviewed"], total, emails))
    rows.sort(key=lambda r: r[3], reverse=True)

    print(f"\nUnattributed contributors ({len(rows)} total), ranked by combined credits:\n")
    print("| Name | Authored | Reviewed | Total | Email(s) |")
    print("|---|---:|---:|---:|---|")
    for name, a, r, total, emails in rows[:top_n]:
        print(f"| {name} | {a} | {r} | {total} | {', '.join(emails) or '-'} |")
    if len(rows) > top_n:
        print(f"\n...and {len(rows) - top_n} more with fewer credits (pass a larger --top-n to see them).")


def print_company_statistics(
    raw: str, after: str, before: str | None, top_n: int, list_unattributed: bool = False
) -> tuple[dict, dict]:
    mentions = collect_all_mentions(raw)
    idx = IdentityIndex(mentions)
    authored, reviewed, unattr_a, unattr_r, unattributed = compute_company_stats(mentions, idx)
    unattr_people = len(unattributed)

    range_label = f"after {after}" + (f" before {before}" if before else "")
    HIGHLIGHT = HIGHLIGHT_COMPANY

    def full_ranking(counts):
        return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)

    def print_markdown(title, counts, total_unattributed):
        full = full_ranking(counts)
        rows = full[:top_n]
        print(f"\n{title} ({range_label})\n")
        print("| Rank | Company | Count |")
        print("|---:|---|---:|")
        for i, (company, count) in enumerate(rows, start=1):
            if company == HIGHLIGHT:
                print(f"| {i} | **{company}** | **{count}** |")
            else:
                print(f"| {i} | {company} | {count} |")
        if HIGHLIGHT not in {c for c, _ in rows}:
            for i, (company, count) in enumerate(full, start=1):
                if company == HIGHLIGHT:
                    print(f"| {i} | **{company}** | **{count}** |")
                    break
            else:
                print(f"| -- | **{HIGHLIGHT}** | **0** |")
        print(f"\n(unattributed / no confirmed employer: {total_unattributed})")

    print_markdown(f"Top {top_n} companies -- commits", authored, unattr_a)
    print_markdown(f"Top {top_n} companies -- reviews", reviewed, unattr_r)
    hint = "" if list_unattributed else " (pass --list-unattributed to see who they are)"
    print(
        f"\n({unattr_people} distinct contributors have no company match -- "
        f"add them to COMPANY_NAME_OVERRIDES, or a new domain to COMPANY_DOMAINS, to include them){hint}"
    )

    if list_unattributed:
        print_unattributed(idx, unattributed, top_n)

    return authored, reviewed


def _company_chart_rows(counts: dict, top_n: int) -> tuple[list, int | None]:
    """Top-N companies by count, with Percona force-included (like the
    Markdown table) even if it falls outside the top N."""
    full = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    rows = full[:top_n]
    if HIGHLIGHT_COMPANY in {c for c, _ in rows}:
        return rows, None
    for rank, (company, count) in enumerate(full, start=1):
        if company == HIGHLIGHT_COMPANY:
            return rows + [(company, count)], rank
    return rows + [(HIGHLIGHT_COMPANY, 0)], None


def write_company_chart(
    authored: dict,
    reviewed: dict,
    top_n: int,
    after: str,
    before: str | None,
    output_path: str,
) -> Path:
    """Render a horizontal bar chart (one panel per metric) as a self-contained
    SVG -- no plotting dependency required, and small enough to drag straight
    into Slack."""
    width        = 760
    margin_left  = 215
    margin_right = 70
    plot_width   = width - margin_left - margin_right
    bar_h        = 22
    row_step     = bar_h + 10
    gap_extra    = 14  # extra space before a force-included Percona row

    colors = {
        "bg":      "#ffffff",
        "text":    "#24292f",
        "axis":    "#6e7781",
        "grid":    "#d0d7de",
        "bar":     "#6da7ec",   # sequential blue, mid step -- other companies
        "bar_hi":  "#1c5cab",   # sequential blue, dark step -- Percona
    }

    panels = [
        ("Top companies — commits", *_company_chart_rows(authored, top_n)),
        ("Top companies — reviews", *_company_chart_rows(reviewed, top_n)),
    ]

    def panel_height(rows, extra_rank):
        n = len(rows)
        extra = gap_extra if extra_rank is not None else 0
        return 44 + n * row_step + extra

    total_height = 70 + sum(panel_height(rows, extra_rank) for _, rows, extra_rank in panels) + 20 * (len(panels) - 1)

    range_label = f"after {after}" + (f" before {before}" if before else "")
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{total_height}" viewBox="0 0 {width} {total_height}">',
        f'<rect width="{width}" height="{total_height}" fill="{colors["bg"]}" />',
        f'<text x="{margin_left}" y="30" font-family="sans-serif" font-size="20" fill="{colors["text"]}">Percona company activity</text>',
        f'<text x="{margin_left}" y="50" font-family="sans-serif" font-size="13" fill="{colors["axis"]}">{escape(range_label)}</text>',
    ]

    panel_top = 70
    for title, rows, extra_rank in panels:
        max_val = max((v for _, v in rows), default=0) or 1
        svg.append(f'<text x="{margin_left}" y="{panel_top}" font-family="sans-serif" font-size="16" fill="{colors["text"]}">{escape(title)}</text>')

        y = panel_top + 20
        for i, (company, count) in enumerate(rows):
            is_last_extra = extra_rank is not None and i == len(rows) - 1
            if is_last_extra:
                y += gap_extra
                sep_y = y - gap_extra / 2
                svg.append(f'<line x1="{margin_left}" y1="{sep_y:.2f}" x2="{width - margin_right}" y2="{sep_y:.2f}" stroke="{colors["grid"]}" stroke-width="1" stroke-dasharray="2,3" />')

            is_hi = company == HIGHLIGHT_COMPANY
            bar_len = (count / max_val) * plot_width if max_val else 0
            color = colors["bar_hi"] if is_hi else colors["bar"]
            rank = extra_rank if is_last_extra and extra_rank is not None else i + 1
            label = f"{rank}. {company}"
            weight = ' font-weight="bold"' if is_hi else ""

            svg.append(f'<rect x="{margin_left}" y="{y:.2f}" width="{max(bar_len, 1):.2f}" height="{bar_h}" rx="4" fill="{color}" />')
            svg.append(f'<text x="{margin_left - 10}" y="{y + bar_h / 2 + 4:.2f}" text-anchor="end" font-family="sans-serif" font-size="13"{weight} fill="{colors["text"]}">{escape(label)}</text>')
            svg.append(f'<text x="{margin_left + bar_len + 8:.2f}" y="{y + bar_h / 2 + 4:.2f}" text-anchor="start" font-family="sans-serif" font-size="13"{weight} fill="{colors["text"]}">{count}</text>')

            y += row_step

        panel_top += panel_height(rows, extra_rank) + 20

    svg.append("</svg>")

    output = Path(output_path)
    output.write_text("\n".join(svg), encoding="utf-8")
    return output


def print_table(
    authored: dict,
    reviewed: dict,
    identities: dict[str, list[str]],
    after: str,
    before: str | None,
) -> None:
    range_label = f"after {after}" + (f" before {before}" if before else "")
    active_people   = [p for p in identities if authored[p] or reviewed[p]]
    authored_people = [p for p in identities if authored[p]]
    reviewed_people = [p for p in identities if reviewed[p]]

    print(f"\nPercona-affiliated activity ({range_label})\n")
    print(f"{'Person':<25} {'Authored':>8} {'Reviewed':>9}")
    print("─" * 46)
    for person in identities:
        print(f"{person:<25} {len(authored[person]):>8} {len(reviewed[person]):>9}")
    print("─" * 46)
    print(f"{'People active in log':<25} {len(active_people):>8} {f'of {len(identities)}':>9}")
    print(f"{'People with authored hits':<25} {len(authored_people):>8} {'':>9}")
    print(f"{'People with reviewed hits':<25} {len(reviewed_people):>8} {'':>9}")
    print()


def _date_range(start: date, end: date) -> list[date]:
    days = (end - start).days
    return [start + timedelta(days=offset) for offset in range(days + 1)]


def _build_daily_counts(subject_dates: dict[str, date], start: date, end: date) -> dict[date, int]:
    daily: dict[date, int] = defaultdict(int)
    for subject_date in subject_dates.values():
        if start <= subject_date <= end:
            daily[subject_date] += 1
    return daily


def _build_cumulative_series(
    subject_dates: dict[str, date], start: date, end: date
) -> tuple[list[date], list[int], list[tuple[date, int]]]:
    dates  = _date_range(start, end)
    daily  = _build_daily_counts(subject_dates, start, end)
    total  = 0
    cumulative: list[int] = []
    event_points: list[tuple[date, int]] = []

    for current in dates:
        increment = daily.get(current, 0)
        total += increment
        cumulative.append(total)
        if increment:
            event_points.append((current, total))

    return dates, cumulative, event_points


def _svg_polyline(points: list[tuple[float, float]]) -> str:
    return " ".join(f"{x:.2f},{y:.2f}" for x, y in points)


def _x_pos(current: date, start: date, span_days: int, left: float, width: float) -> float:
    if span_days <= 0:
        return left + width / 2
    return left + (((current - start).days / span_days) * width)


def _y_pos(value: int, y_max: int, top: float, height: float) -> float:
    if y_max <= 0:
        return top + height
    return top + height - ((value / y_max) * height)


def _tick_values(y_max: int, target_ticks: int = 6) -> list[int]:
    if y_max <= 0:
        return [0]
    step = max(1, (y_max + target_ticks - 1) // target_ticks)
    ticks = list(range(0, y_max + 1, step))
    if ticks[-1] != y_max:
        ticks.append(y_max)
    return ticks


def write_timeline_graph(
    authored_dates: dict,
    reviewed_dates: dict,
    identities: dict[str, list[str]],
    after: str,
    before: str | None,
    output_path: str,
) -> Path:
    start = datetime.strptime(after, "%Y-%m-%d").date()
    end   = datetime.strptime(before, "%Y-%m-%d").date() if before else date.today()
    if end < start:
        raise ValueError("--before must be on or after --after")

    panel_height = 220
    panel_gap    = 36
    width        = 1200
    height       = 90 + len(identities) * (panel_height + panel_gap)
    margin_left  = 90
    margin_right = 30
    plot_width   = width - margin_left - margin_right
    plot_height  = 140
    span_days    = max((end - start).days, 0)

    series_data: dict = {}
    for person in identities:
        authored_series = _build_cumulative_series(authored_dates[person], start, end)
        reviewed_series = _build_cumulative_series(reviewed_dates[person], start, end)
        local_max = max(max(authored_series[1], default=0), max(reviewed_series[1], default=0), 1)
        series_data[person] = {
            "authored": authored_series,
            "reviewed": reviewed_series,
            "max": local_max,
        }

    tick_count   = min(6, span_days + 1) if span_days >= 0 else 1
    tick_offsets = {round(i * span_days / max(tick_count - 1, 1)) for i in range(tick_count)}
    tick_dates   = [start + timedelta(days=offset) for offset in sorted(tick_offsets)]

    colors = {
        "authored": "#1f77b4",
        "reviewed": "#d62728",
        "grid":     "#d0d7de",
        "axis":     "#6e7781",
        "text":     "#24292f",
        "bg":       "#ffffff",
    }

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        f'<rect width="{width}" height="{height}" fill="{colors["bg"]}" />',
        f'<text x="{margin_left}" y="34" font-family="sans-serif" font-size="24" fill="{colors["text"]}">Percona activity timeline</text>',
        f'<text x="{margin_left}" y="58" font-family="sans-serif" font-size="14" fill="{colors["axis"]}">{start.isoformat()} to {end.isoformat()}</text>',
        f'<text x="{width - 280}" y="34" font-family="sans-serif" font-size="14" fill="{colors["authored"]}">Authored</text>',
        f'<text x="{width - 200}" y="34" font-family="sans-serif" font-size="14" fill="{colors["reviewed"]}">Reviewed</text>',
    ]

    for index, person in enumerate(identities):
        panel_top    = 90 + index * (panel_height + panel_gap)
        chart_top    = panel_top + 28
        chart_bottom = chart_top + plot_height
        y_max        = max(series_data[person]["max"], 1)
        y_ticks      = _tick_values(y_max)

        svg.append(f'<text x="{margin_left}" y="{panel_top}" font-family="sans-serif" font-size="18" fill="{colors["text"]}">{escape(person)}</text>')

        for step in y_ticks:
            y = _y_pos(step, y_max, chart_top, plot_height)
            svg.append(f'<line x1="{margin_left}" y1="{y:.2f}" x2="{width - margin_right}" y2="{y:.2f}" stroke="{colors["grid"]}" stroke-width="1" />')
            svg.append(f'<text x="{margin_left - 10}" y="{y + 4:.2f}" text-anchor="end" font-family="sans-serif" font-size="12" fill="{colors["axis"]}">{step}</text>')

        svg.append(f'<line x1="{margin_left}" y1="{chart_bottom:.2f}" x2="{width - margin_right}" y2="{chart_bottom:.2f}" stroke="{colors["axis"]}" stroke-width="1.5" />')
        svg.append(f'<line x1="{margin_left}" y1="{chart_top:.2f}" x2="{margin_left}" y2="{chart_bottom:.2f}" stroke="{colors["axis"]}" stroke-width="1.5" />')

        for tick_date in tick_dates:
            x = _x_pos(tick_date, start, span_days, margin_left, plot_width)
            svg.append(f'<line x1="{x:.2f}" y1="{chart_top:.2f}" x2="{x:.2f}" y2="{chart_bottom:.2f}" stroke="{colors["grid"]}" stroke-width="1" />')
            svg.append(f'<text x="{x:.2f}" y="{chart_bottom + 18:.2f}" text-anchor="middle" font-family="sans-serif" font-size="12" fill="{colors["axis"]}">{tick_date.isoformat()}</text>')

        for label, color in (("authored", colors["authored"]), ("reviewed", colors["reviewed"])):
            dates, values, event_points = series_data[person][label]
            points = [
                (
                    _x_pos(current, start, span_days, margin_left, plot_width),
                    _y_pos(value, y_max, chart_top, plot_height),
                )
                for current, value in zip(dates, values)
            ]
            svg.append(f'<polyline fill="none" stroke="{color}" stroke-width="3" points="{_svg_polyline(points)}" />')
            for event_date, value in event_points:
                x = _x_pos(event_date, start, span_days, margin_left, plot_width)
                y = _y_pos(value, y_max, chart_top, plot_height)
                svg.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4" fill="{color}" />')

    svg.append("</svg>")

    output = Path(output_path)
    output.write_text("\n".join(svg), encoding="utf-8")
    return output


HELP_EXTRA = """
Modifying this script
─────────────────────
Percona contributors are discovered automatically:
  Every @percona.com address found in Author:, Co-authored-by:, or
  Reviewed-by: trailers within the requested date range is included
  automatically — no manual update needed when someone new joins.

Add a non-Percona person (or override a discovered entry):
  Edit the MANUAL_IDENTITIES dict near the top of the file. Each key is the
  display name; the value is a list of lowercase fragments matched against the
  email or name in commit trailers.

  Example — add Peter Eisentraut:
    "Peter Eisentraut": ["peter@eisentraut.org", "peter eisentraut"],

Change the default date range:
  Edit DEFAULT_AFTER / DEFAULT_BEFORE near the top of the file, or just
  pass --after / --before on the command line each time.

Show a graph:
  Pass --graph to write an SVG timeline graph with cumulative authored and
  reviewed lines per person. Use --graph-output to choose the filename.

Company statistics (top companies, not just Percona):
  Pass --company-statistics to print Markdown tables (paste-ready for e.g.
  Confluence) ranking every company by commits and by reviews in the given
  date range -- not limited to Percona. Uses --top-n to control how many
  companies are shown (default 20).

  Pass --company-chart alongside it to also write an SVG bar chart of the
  same commit/review counts per company (--company-chart-output to choose the
  filename) -- small and self-contained, so it can be dragged straight into
  Slack instead of pasting the Markdown tables.

  Every contributor is grouped by company two ways:
    1. Corporate email domain -> company, via the COMPANY_DOMAINS dict
       (e.g. @enterprisedb.com -> EDB, @fujitsu.com -> Fujitsu).
    2. A manual override for people posting from a personal-looking address
       whose employer is known from public bios, via COMPANY_NAME_OVERRIDES
       (e.g. Tom Lane -> Snowflake). Update this dict as people change jobs.
  Contributors matching neither are reported as "unattributed" rather than
  guessed at -- add them to one of the two dicts to include them. Pass
  --list-unattributed to print who they actually are (name, email(s), and
  commit/review counts), ranked by impact, so you know who's worth
  researching first instead of guessing at 200+ names blind.

  This mode also fixes a parsing gap in the rest of the script: roughly a
  third of all Reviewed-by trailers in this repo are bare comma-separated
  names with no email at all (e.g. "Reviewed-by: Name1, Name2"), which the
  email-only regex used elsewhere in this file silently drops.

How counting works:
  - Backports (same subject, different commit hash) count as ONE patch.
  - "Authored"  = appears in Author: or Co-authored-by: trailer.
  - "Reviewed"  = appears in Reviewed-by: trailer.
  - A commit where someone is both author and reviewer counts in both columns.
"""


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__ + HELP_EXTRA,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--after",  default=DEFAULT_AFTER,  metavar="DATE",
                        help="Include commits after this date (default: %(default)s)")
    parser.add_argument("--before", default=DEFAULT_BEFORE, metavar="DATE",
                        help="Include commits before this date (default: HEAD)")
    parser.add_argument("--graph", action="store_true",
                        help="Write an SVG timeline graph")
    parser.add_argument("--graph-output", default=DEFAULT_GRAPH_OUTPUT, metavar="PATH",
                        help="Output path for --graph (default: %(default)s)")
    parser.add_argument("--company-statistics", action="store_true",
                        help="Print top companies by commits/reviews (Markdown tables, "
                             "paste-ready for e.g. Confluence) instead of the per-person table")
    parser.add_argument("--top-n", type=int, default=DEFAULT_COMPANY_TOP_N, metavar="N",
                        help="Number of companies to show with --company-statistics (default: %(default)s)")
    parser.add_argument("--list-unattributed", action="store_true",
                        help="With --company-statistics, also list which contributors have no "
                             "company match, ranked by credits, so you know who to research next")
    parser.add_argument("--company-chart", action="store_true",
                        help="With --company-statistics, also write an SVG bar chart of the "
                             "same commit/review counts per company (easy to drag into Slack)")
    parser.add_argument("--company-chart-output", default=DEFAULT_COMPANY_CHART_OUTPUT, metavar="PATH",
                        help="Output path for --company-chart (default: %(default)s)")
    args = parser.parse_args()

    raw = fetch_log(args.after, args.before)

    if args.company_statistics:
        authored, reviewed = print_company_statistics(
            raw, args.after, args.before, args.top_n, args.list_unattributed
        )
        if args.company_chart:
            output = write_company_chart(
                authored, reviewed, args.top_n, args.after, args.before, args.company_chart_output
            )
            print(f"\nCompany chart written to {output}")
        return

    identities = build_identities(raw)
    authored, reviewed, authored_dates, reviewed_dates = parse_log(raw, identities)
    print_table(authored, reviewed, identities, args.after, args.before)

    if args.graph:
        output = write_timeline_graph(
            authored_dates,
            reviewed_dates,
            identities,
            args.after,
            args.before,
            args.graph_output,
        )
        print(f"Timeline graph written to {output}")


if __name__ == "__main__":
    main()
