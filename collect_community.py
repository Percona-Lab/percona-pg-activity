#!/usr/bin/env python3
"""
Collect Percona's PostgreSQL community activity into community.json for the
dashboard: conference talks, organiser / program / code-of-conduct committee
roles, volunteering, blog posts, Planet PostgreSQL syndication, mailing-list
activity (Hackorum), postgresql.org contributor and release-note recognition.

    python3 collect_community.py --output site/community.json

People to look for: community/people.json. Hand-curated entries (meetups,
volunteering and anything no website lists): community/manual.json.
HTTP responses are cached under .cache/http: pages of past events for a year,
everything else for about a day, so daily runs only fetch what may change.
"""

import argparse
import hashlib
import html
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

ROOT = Path(__file__).resolve().parent
COMPANY = "Percona"
START_YEAR = 2022
USER_AGENT = "percona-pg-activity/1.0 (+https://github.com/Percona-Lab/percona-pg-activity)"
REQUEST_GAP = 0.5            # seconds between live requests to the same host
DISCOVERY_DAYS = 550         # speaker profiles show the *current* employer, so only
                             # trust them for events this recent

# pgeu-system instances. Their event index pages link each conference's own
# website; the website links back to /events/<slug>/ on the instance. When it
# doesn't, "guess" maps the website's domain to a slug prefix (+ year).
PGEU_INSTANCES = [
    {"base": "https://www.postgresql.eu", "index": ["/events/", "/events/past/"], "guess": {}},
    {"base": "https://postgresql.us", "index": ["/events/", "/events/past/"],
     "guess": {"pgconf.nyc": "pgconfnyc", "postgressummit.us": "postgressummitus", "pgdaychicago.org": "pgdaychicago"}},
    {"base": "https://pgday.uk", "index": ["/events/", "/events/past/"], "guess": {"pgday.uk": "pgdayuk"}},
    {"base": "https://www.pgevents.ca", "index": ["/events/", "/events/past/"], "guess": {"pgconf.dev": "pgconfdev"}},
]

PERCONA_BLOG_FEED = "https://www.percona.com/blog/category/postgresql/feed/?paged={page}"
COMMUNITY_BLOG_FEED = "https://percona.community/blog/index.xml"
COMMUNITY_PG_CATEGORIES = {"postgresql", "postgres", "percona server for postgresql", "pg_tde"}
PLANET = "https://planet.postgresql.org/"
HACKORUM_TEAM = "https://hackorum.dev/team/percona"
CONTRIBUTORS = "https://www.postgresql.org/community/contributors/"
RELEASE_NOTES = "https://www.postgresql.org/docs/release/{v}.0/"


# ── HTTP with an on-disk cache ──────────────────────────────────────────────

class Http:
    def __init__(self, cache_dir: Path, offline: bool = False):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.offline = offline
        self.last_hit: dict[str, float] = {}
        self.stats = {"cached": 0, "fetched": 0, "failed": 0}

    def get(self, url: str, max_age_days: float = 0.9, probe: bool = False) -> str | None:
        """Fetch url, from cache when fresh. probe=True is for guessed URLs:
        any failure is remembered quietly as "not there"."""
        path = self.cache_dir / hashlib.sha1(url.encode()).hexdigest()
        if path.exists():
            age = (time.time() - path.stat().st_mtime) / 86400
            if age < max_age_days or self.offline:
                self.stats["cached"] += 1
                body = path.read_text(encoding="utf-8")
                return None if body == "\0404" else body
        if self.offline:
            return None
        host = urlparse(url).netloc
        wait = self.last_hit.get(host, 0) + REQUEST_GAP - time.time()
        if wait > 0:
            time.sleep(wait)
        self.last_hit[host] = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code in (404, 410) or probe:
                path.write_text("\0404", encoding="utf-8")   # remember misses too
                self.stats["fetched"] += 1
                return None
            return self._stale(path, url, f"HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if probe:
                path.write_text("\0404", encoding="utf-8")
                return None
            return self._stale(path, url, str(e))
        self.stats["fetched"] += 1
        path.write_text(body, encoding="utf-8")
        return body

    def _stale(self, path: Path, url: str, why: str) -> str | None:
        self.stats["failed"] += 1
        print(f"  ! {url}: {why}", file=sys.stderr)
        if path.exists():
            body = path.read_text(encoding="utf-8")
            return None if body == "\0404" else body
        return None


# ── People ──────────────────────────────────────────────────────────────────

TRANSLIT = str.maketrans({"ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "æ": "ae", "ß": "ss", "đ": "d"})


def norm(name: str) -> str:
    name = html.unescape(name).translate(TRANSLIT)
    name = "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))
    name = re.sub(r"\(.*?\)|<[^>]*>", " ", name)
    return re.sub(r"[^a-z ]+", "", re.sub(r"\s+", " ", name.lower())).strip()


def clean_display(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>|\(.*?\)", " ", html.unescape(name))).strip()


NO_COMPANY = {"", "none", "n/a", "-", "self", "self-employed"}


class Roster:
    def __init__(self, path: Path):
        data = json.loads(path.read_text())
        self.people = data["people"]
        self.excluded = {norm(p["name"]) for p in data.get("exclude", [])}
        self.by_key = {}
        for p in self.people:
            for n in [p["name"], *p.get("aliases", [])]:
                self.by_key[norm(n)] = p
        self.review = []          # roster matches we dropped, for maintainers

    def known(self, name: str) -> bool:
        return norm(name) in self.by_key

    def active(self, name: str, when: str | None) -> bool:
        p = self.by_key[norm(name)]
        return not when or p.get("since", "0000") <= when <= p.get("until", "9999")

    def match(self, name: str, company: str | None, when: str | None, where: str = "") -> tuple[str, str] | None:
        """(canonical name, evidence) if this person counts as Percona here.

        A source naming Percona as the company is enough. Otherwise the person
        must be in people.json, the date inside their since/until window, and
        the source must not name a different employer."""
        company = (company or "").strip()
        listed = COMPANY.lower() in company.lower()
        other = not listed and company.lower() not in NO_COMPANY
        if norm(name) in self.excluded:
            return None
        p = self.by_key.get(norm(name))
        if p and when and (p.get("since", "0000") > when or p.get("until", "9999") < when):
            self.review.append({"person": p["name"], "where": where, "date": when, "reason": "outside since/until"})
            return None
        if listed:
            return (p["name"] if p else clean_display(name)), "company listed"
        if not p:
            return None
        if other:
            self.review.append({"person": p["name"], "where": where, "date": when, "reason": f"listed as {company}"})
            return None
        return p["name"], "people.json"


# ── HTML helpers ────────────────────────────────────────────────────────────

def strip_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def text_of(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


class PeopleSections(HTMLParser):
    """(section, name, company) rows from conference organisation pages.
    Handles the pgeu template (class="name" / class="company" on any tag) and
    the "Name<br><small>Company</small>" captions PGConf.dev uses. Sections
    come from non-empty h1-h3 headings, or a wrapper's class ("committe
    organisers") when a template leaves the heading empty."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.section = ""
        self.capture = None      # heading | name | company
        self.cap_tag = None
        self.buf = []
        self.rows = []
        self.in_caption = False

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get("class") or ""
        if tag in ("section", "div") and re.search(r"committe|organi[sz]er|volunteer|program", cls):
            self.section = "class:" + cls
        if "caption" in cls:
            self.in_caption = True
        if tag in ("h1", "h2", "h3") and not re.search(r"\bname\b", cls):
            self._start("heading", tag)
        elif re.search(r"\bname\b", cls):
            self._start("name", tag)
        elif re.search(r"\bcompany\b", cls) or (tag == "small" and self.rows and self.rows[-1][2] is None and self.capture is None):
            self._start("company", tag)
        elif tag == "figcaption" or (tag == "h4" and self.in_caption):
            self._start("name", tag)
        elif tag == "br" and self.capture == "name" and self.cap_tag in ("figcaption", "h4") and not re.search(r"\(", "".join(self.buf)):
            self._finish_name()

    def _start(self, what, tag):
        self.capture, self.cap_tag, self.buf = what, tag, []

    def _finish_name(self):
        name = " ".join("".join(self.buf).split())
        if name:
            self.rows.append([self.section, name, None])
        self.capture = None

    def handle_endtag(self, tag):
        if tag in ("figcaption", "h4"):
            self.in_caption = False if tag == "figcaption" else self.in_caption
        if not self.capture or tag != self.cap_tag:
            return
        text = " ".join("".join(self.buf).split())
        if self.capture == "heading":
            if text:
                self.section = text
        elif self.capture == "name":
            self._finish_name()
            return
        elif self.capture == "company" and self.rows and self.rows[-1][2] is None:
            self.rows[-1][2] = text
        self.capture = None

    def handle_data(self, data):
        if self.capture:
            self.buf.append(data)


def section_role(section: str) -> str | None:
    s = section.lower()
    if "code of conduct" in s or "coc" in s:
        return "coc-committee"
    if re.search(r"program|talk selection|paper|cfp", s):
        return "program-committee"
    if "volunteer" in s:
        return "volunteer"
    if re.search(r"organi|committe|team|operations|planning", s):
        return "organiser"
    return None


NON_TALKS = re.compile(r"^(opening|closing|welcome|registration|lunch|coffee|break|keynote introduction)\b", re.I)


# ── pgeu-system conferences ────────────────────────────────────────────────

SITE_RE = re.compile(r'href="(https?://(?:(\d{4})\.[a-z0-9.-]+|fosdem(\d{4})\.pgconf\.eu)/?)[^"]*"')


def conference_sites(http: Http, inst: dict) -> list[tuple[str, int]]:
    sites = {}
    for path in inst["index"]:
        page = http.get(inst["base"] + path) or ""
        for m in SITE_RE.finditer(page):
            url = m.group(1).rstrip("/") + "/"
            year = int(m.group(2) or m.group(3))
            if START_YEAR <= year <= date.today().year + 1:
                sites[url] = year
    # Index pages can lag (2026.pgconf.de was linked nowhere while its schedule
    # was live), so also probe this and next year for every yearly domain seen.
    for url in list(sites):
        m = re.match(r"https?://\d{4}\.([a-z0-9.-]+)/$", url)
        if m:
            for year in (date.today().year, date.today().year + 1):
                guess = f"https://{year}.{m.group(1)}/"
                if guess not in sites and http.get(guess, max_age_days=7, probe=True) is not None:
                    sites[guess] = year
    return sorted(sites.items(), key=lambda kv: (kv[1], kv[0]))


def is_past(day: str | None) -> bool:
    return bool(day) and day < (date.today() - timedelta(days=14)).isoformat()


def add_event(out: dict, ev: dict) -> None:
    out["events"].setdefault(ev["id"], ev)


def collect_pgeu(http: Http, roster: Roster, inst: dict, out: dict) -> None:
    base = inst["base"]
    host = urlparse(base).netloc
    slug_re = re.compile(re.escape(host) + r"/events/(?:(?:register|schedule|sponsor/signup)/)?([a-z]+\d{4})/")
    for site, year in conference_sites(http, inst):
        old = year < date.today().year
        home = http.get(site, max_age_days=365 if old else 0.9)
        if home is None:
            continue
        slugs = slug_re.findall(home)
        slug = max(set(slugs), key=slugs.count) if slugs else None
        if not slug:
            domain = urlparse(site).netloc.split(".", 1)[1]
            prefix = inst["guess"].get(domain)
            slug = f"{prefix}{year}" if prefix else None
        if not slug:
            continue
        xml_text = http.get(f"{base}/events/{slug}/schedule.xml", max_age_days=365 if old else 0.9)
        conf = {"id": slug, "url": site, "source": host}
        sessions = parse_frab(xml_text, conf)
        if "name" not in conf:
            title = re.search(r"<title>(.*?)</title>", home, re.S)
            conf.update(name=text_of(title.group(1)) if title else site, start=f"{year}-01-01", end=f"{year}-01-01", date_unknown=True)
        past = is_past(conf["end"])
        add_event(out, conf)
        recent = conf["end"] >= (date.today() - timedelta(days=DISCOVERY_DAYS)).isoformat()

        companies = {}
        for s in sessions:
            if NON_TALKS.search(s["title"]):
                continue
            for pid, pname in s["persons"]:
                company = None
                if recent or roster.known(pname):
                    if pid not in companies:
                        page = http.get(f"{base}/events/{slug}/schedule/speaker/{pid}/", max_age_days=365 if past else 7) or ""
                        m = re.search(r'<span class="company">(.*?)</span>|<b>Company:</b>\s*([^<]+)', page, re.S)
                        companies[pid] = text_of(m.group(1) or m.group(2)) if m else None
                    # A profile shows today's employer: only use it as evidence for recent events.
                    company = companies[pid] if recent else None
                hit = roster.match(pname, company, s["day"] or conf["start"], f"{slug} talk")
                if hit:
                    add_talk(http, out, hit, company, slug, s, pname, past)

        collect_org_page(http, roster, out, site, home, conf, past)
        n = sum(1 for a in out["activities"] if a["event"] == slug)
        print(f"  {slug:<22} {conf['start']}  sessions={len(sessions):<3} percona={n}")


def parse_frab(xml_text: str | None, conf: dict) -> list[dict]:
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text.encode())
    except ET.ParseError:
        return []
    c = root.find("conference")
    if c is not None and c.findtext("start"):
        conf.update(name=(c.findtext("title") or conf["id"]).strip(), start=c.findtext("start"), end=c.findtext("end") or c.findtext("start"))
    sessions = []
    for day in root.iter("day"):
        for ev in day.iter("event"):
            persons = [(p.get("id"), (p.text or "").strip()) for p in ev.iter("person")]
            if persons:
                sessions.append({"title": " ".join((ev.findtext("title") or "").split()), "url": ev.findtext("url"),
                                 "track": ev.findtext("track"), "day": day.get("date"), "persons": persons})
    return sessions


def add_talk(http, out, hit, company, event_id, s, pname, past):
    slides, video = session_links(http, s.get("url"), past)
    out["activities"].append({
        "kind": "talk", "person": hit[0], "evidence": hit[1], "company_listed": company,
        "event": event_id, "date": s.get("day"), "title": s["title"], "url": s.get("url"),
        "track": s.get("track"), "slides": slides, "video": video,
        "co_speakers": [n for _, n in s["persons"] if norm(n) != norm(pname)],
    })


def session_links(http: Http, url: str | None, past: bool) -> tuple[str | None, str | None]:
    if not url or "/session/" not in url:
        return None, None
    page = http.get(url, max_age_days=60 if past else 3) or ""
    slides = re.search(r'class="icon slides" href="([^"]+)"|href="([^"]+)"[^>]*class="[^"]*slides', page)
    video = re.search(r'href="(https://(?:www\.)?(?:youtube\.com/watch|youtu\.be/|vimeo\.com/)[^"]+)"', page)
    return (html.unescape(slides.group(1) or slides.group(2)) if slides else None,
            html.unescape(video.group(1)) if video else None)


def collect_org_page(http, roster, out, site, home, conf, past, extra_urls=()):
    links = re.findall(r'href="([^"#]*organi[sz]ation[^"#]*)"', home) or re.findall(r'href="([^"#]*/about(?:\.html|/)?)"', home)
    candidates = [urljoin(site, links[0])] if links else [urljoin(site, "organisation/"), urljoin(site, "organization/"), urljoin(site, "about")]
    seen = set()
    for url in [*candidates, *extra_urls]:
        page = http.get(url, max_age_days=365 if past else 0.9)
        if not page:
            continue
        parser = PeopleSections()
        parser.feed(strip_comments(page))
        for section, name, company in parser.rows:
            role = section_role(section)
            if not role:
                continue
            hit = roster.match(name, company, conf["start"], f"{conf['id']} {role}")
            if not hit or (hit[0], role) in seen:
                continue
            seen.add((hit[0], role))
            note = re.search(r"\((chair[^)]*)\)", name)
            out["activities"].append({
                "kind": role, "person": hit[0], "evidence": hit[1], "company_listed": company,
                "event": conf["id"], "date": conf["start"], "url": url, "section": None if section.startswith("class:") else section,
                "note": note.group(1) if note else None,
            })
        if links or seen:
            break


# ── Swiss PGDay (own site) ──────────────────────────────────────────────────

def collect_swiss(http: Http, roster: Roster, out: dict) -> None:
    for year in range(START_YEAR, date.today().year + 2):
        old = year < date.today().year
        ev_id = f"swisspgday{year}"
        legacy = http.get(f"https://www.pgday.ch/{year}/", max_age_days=365 if old else 0.9, probe=True)
        if legacy and "track" in legacy and "dialog" in legacy:
            # "Thursday, 27 June and Friday, 28 June 2024" / "Thursday, June 29 and Friday, June 30"
            m = (re.search(r"day,\s*(\d{1,2})\s+(June|July)\s+and", legacy)
                 or re.search(r"day,\s*(June|July)\s+(\d{1,2})\s+and", legacy))
            start = f"{year}-06-01"
            if m:
                day_s, month_s = (m.group(1), m.group(2)) if m.group(1).isdigit() else (m.group(2), m.group(1))
                start = f"{year}-{6 if month_s == 'June' else 7:02d}-{int(day_s):02d}"
            conf = {"id": ev_id, "name": f"Swiss PGDay {year}", "start": start, "end": start, "url": f"https://www.pgday.ch/{year}/", "source": "pgday.ch"}
            add_event(out, conf)
            for sm in re.finditer(r'<i>([^<]+)</i>,\s*<a[^>]*>(?:<span[^>]*></span>)?([^<]+)</a>\s*<div id="([^"]+)">(.*?)</div>', legacy, re.S):
                names, title, _, body = sm.groups()
                companies = dict((text_of(a), text_of(b)) for a, b in re.findall(r"<p>([^<,]+),\s*([^<]+)</p>", body))
                persons = [(None, n.strip()) for n in re.split(r",| and | und ", html.unescape(names)) if n.strip()]
                s = {"title": text_of(title), "url": conf["url"], "day": start, "persons": persons}
                for _, pname in persons:
                    hit = roster.match(pname, companies.get(pname), start, f"{ev_id} talk")
                    if hit:
                        add_talk(http, out, hit, companies.get(pname), ev_id, s, pname, True)
            print(f"  {ev_id:<22} {start}  percona={sum(1 for a in out['activities'] if a['event'] == ev_id)}")
            continue
        site = f"https://{year}.pgday.ch/"
        home = http.get(site, max_age_days=365 if old else 0.9, probe=year > date.today().year)
        if not home:
            continue
        sched = http.get(site + "schedule/", max_age_days=365 if old else 0.9) or ""
        dm = re.search(r"(\d{1,2})\s+(June|July)\s+(\d{4})", home)
        start = datetime.strptime(" ".join(dm.groups()), "%d %B %Y").date().isoformat() if dm else f"{year}-06-01"
        conf = {"id": ev_id, "name": f"Swiss PGDay {year}", "start": start, "end": start, "url": site, "source": "pgday.ch"}
        add_event(out, conf)
        for block in re.split(r'<details class="session', sched)[1:]:
            tm = re.search(r'session-title"><a href="([^"]+)">(.*?)</a>', block, re.S)
            if not tm or NON_TALKS.search(text_of(tm.group(2))):
                continue
            people = re.findall(r'<span class="name">(.*?)</span>\s*(?:<span class="company">(.*?)</span>)?', block, re.S)
            s = {"title": text_of(tm.group(2)), "url": tm.group(1), "day": start, "persons": [(None, text_of(n)) for n, _ in people]}
            for n, c in people:
                hit = roster.match(text_of(n), text_of(c), start, f"{ev_id} talk")
                if hit:
                    add_talk(http, out, hit, text_of(c), ev_id, s, text_of(n), is_past(start))
        collect_org_page(http, roster, out, site, home, conf, is_past(start), extra_urls=[site + "call-for-papers/"])
        print(f"  {ev_id:<22} {start}  percona={sum(1 for a in out['activities'] if a['event'] == ev_id)}")


# ── Blogs ───────────────────────────────────────────────────────────────────

NS = {"dc": "http://purl.org/dc/elements/1.1/", "content": "http://purl.org/rss/1.0/modules/content/"}


def rss_items(xml_text: str | None):
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text.encode())
    except ET.ParseError:
        return []
    return root.iter("item")


def parse_date(s: str | None) -> str | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s.replace("UTC", "+0000")).date().isoformat()
    except (TypeError, ValueError):
        m = re.search(r"\d{4}-\d{2}-\d{2}", s)
        return m.group(0) if m else None


def excerpt(fragment: str | None, n: int = 260) -> str:
    t = text_of(fragment or "")
    t = re.sub(r"The post .* appeared first on .*$", "", t).strip()
    return t if len(t) <= n else t[: n].rsplit(" ", 1)[0] + "…"


def collect_blogs(http: Http, roster: Roster, out: dict) -> None:
    cutoff = f"{START_YEAR}-01-01"
    seen = set()
    for page in range(1, 80):
        items = list(rss_items(http.get(PERCONA_BLOG_FEED.format(page=page), max_age_days=0.9 if page <= 2 else 14)))
        if not items:
            break
        oldest = None
        for it in items:
            link, day = it.findtext("link"), parse_date(it.findtext("pubDate"))
            oldest = min(oldest or day, day) if day else oldest
            if not link or link in seen or not day or day < cutoff:
                continue
            seen.add(link)
            cats = [c.text or "" for c in it.findall("category")]
            author = it.findtext("dc:creator", namespaces=NS) or ""
            out["posts"].append({
                "source": "percona.com", "title": text_of(it.findtext("title")), "url": link, "date": day,
                "author": roster_name(roster, author), "on_planet": any(re.search(r"\bPP\b|-pp$|planet", c, re.I) for c in cats),
                "excerpt": excerpt(it.findtext("description")),
            })
        if oldest and oldest < cutoff:
            break
    n1 = len(out["posts"])
    for it in rss_items(http.get(COMMUNITY_BLOG_FEED)):
        cats = {(c.text or "").strip().lower() for c in it.findall("category")}
        link, day = it.findtext("link"), parse_date(it.findtext("pubDate"))
        if not cats & COMMUNITY_PG_CATEGORIES or not link or "/blog/" not in link or not day or day < cutoff or link in seen:
            continue
        seen.add(link)
        out["posts"].append({
            "source": "percona.community", "title": text_of(it.findtext("title")), "url": link, "date": day,
            "author": roster_name(roster, it.findtext("author") or it.findtext("dc:creator", namespaces=NS) or ""),
            "on_planet": None, "excerpt": excerpt(it.findtext("description")),
        })
    print(f"  blogs: percona.com={n1} percona.community={len(out['posts']) - n1}")


def roster_name(roster: Roster, author: str) -> str:
    author = text_of(author)
    p = roster.by_key.get(norm(author))
    return p["name"] if p else author


# ── Planet PostgreSQL, Hackorum, postgresql.org recognition ─────────────────

def collect_planet(http: Http, roster: Roster, out: dict) -> None:
    feeds = http.get(PLANET + "feeds.html") or ""
    names = set()
    for feed, site, name in re.findall(r'<li><a href="([^"]+)"><img[^>]*></a>\s*<a href="([^"]+)">([^<]+)</a></li>', feeds):
        if "percona" in (feed + site).lower():
            names.add(roster_name(roster, name))
    home = http.get(PLANET) or ""
    m = re.search(r'<a href="[^"]*percona[^"]*">Percona</a>\s*-\s*(\d+)', home, re.I)
    out["planet"] = {"feeds": sorted(names), "posts_last_two_months": int(m.group(1)) if m else None, "url": PLANET}
    print(f"  planet: {len(names)} Percona feeds, top-teams count {out['planet']['posts_last_two_months']}")


def collect_hackorum(http: Http, out: dict) -> None:
    page = http.get(HACKORUM_TEAM)
    if not page:
        return
    stats = {text_of(l): int(text_of(v).replace(",", "")) for v, l in
             re.findall(r'<span class="stat-value">(.*?)</span>\s*<span class="stat-label">(.*?)</span>', page, re.S)
             if text_of(v).replace(",", "").isdigit()}
    credits = {text_of(l): int(text_of(v).replace(",", "")) for v, l in
               re.findall(r'class="credit-item-value">(.*?)<.*?class="credit-item-label">(.*?)<', page, re.S)
               if text_of(v).replace(",", "").isdigit()}
    by_year = {}
    for year in range(START_YEAR, date.today().year + 1):
        yp = http.get(f"{HACKORUM_TEAM}/contributions/{year}", max_age_days=365 if year < date.today().year else 0.9) or ""
        by_year[str(year)] = sum(int(n) for n in re.findall(r'title="\d{4}-\d{2}-\d{2}: (\d+) messages?"', yp))
    out["hackorum"] = {"url": HACKORUM_TEAM, "totals": stats, "commit_credits": credits, "messages_by_year": by_year}
    print(f"  hackorum: {stats.get('Messages sent')} messages lifetime, by year {by_year}")


def collect_recognition(http: Http, roster: Roster, out: dict) -> None:
    rec = []
    page = http.get(CONTRIBUTORS) or ""
    for chunk in re.split(r"<h2[^>]*>", page)[1:]:
        section = text_of(chunk.split("</h2>", 1)[0])
        for td in re.findall(r"<td[^>]*>(.*?)</td>", chunk, re.S):
            first = text_of(re.split(r"<br\s*/?>", td)[0])
            name = re.sub(r"\s*\(.*$", "", first)
            if roster.known(name):
                rec.append({"kind": "contributor", "person": roster.by_key[norm(name)]["name"], "title": section, "url": CONTRIBUTORS})
    for v in range(16, 40):
        notes = http.get(RELEASE_NOTES.format(v=v), max_age_days=30 if v < 19 else 0.9)
        if notes is None:
            break
        rd = re.search(r"Release date:(?:&nbsp;|\s)*</strong>\s*([0-9?-]+)", notes)
        ack = re.search(rf'id="RELEASE-{v}-ACKNOWLEDGEMENTS".*?class="simplelist">(.*?)</table>', notes, re.S)
        names = [text_of(n) for n in re.findall(r"<td>(.*?)</td>", ack.group(1), re.S)] if ack else []
        names = [n for n in names if n and "fill in later" not in n.lower()]
        if names:
            for n in names:
                if roster.known(n) and roster.active(n, rd.group(1) if rd and "?" not in rd.group(1) else None):
                    rec.append({"kind": "release-ack", "person": roster.by_key[norm(n)]["name"], "title": f"PostgreSQL {v}",
                                "url": RELEASE_NOTES.format(v=v), "date": rd.group(1) if rd else None})
        else:   # notes not finalised yet: count inline per-item credits instead
            counts = {}
            for para in re.findall(r"<p>(.*?)</p>", notes, re.S):
                credit = re.search(r"\(([^()]*)\)\s*<a [^>]*postgr\.es/c/", para)
                if credit:
                    for n in credit.group(1).split(","):
                        if roster.known(n.strip()):
                            key = roster.by_key[norm(n.strip())]["name"]
                            counts[key] = counts.get(key, 0) + 1
            for person, n in counts.items():
                rec.append({"kind": "release-ack", "person": person, "title": f"PostgreSQL {v}", "items": n,
                            "url": RELEASE_NOTES.format(v=v), "date": None, "preliminary": True})
    out["recognition"] = rec
    print(f"  recognition: {len(rec)} entries")


def github_stats(http: Http, repo: str | None) -> dict:
    m = re.match(r"https://github\.com/([^/]+/[^/]+)", repo or "")
    if not m:
        return {}
    body = http.get(f"https://api.github.com/repos/{m.group(1)}")
    try:
        info = json.loads(body or "{}")
    except ValueError:
        return {}
    return {"stars": info.get("stargazers_count"), "pushed_at": (info.get("pushed_at") or "")[:10] or None}


# ── main ────────────────────────────────────────────────────────────────────

def build(http: Http) -> dict:
    roster = Roster(ROOT / "community" / "people.json")
    out = {"events": {}, "activities": [], "roles": [], "projects": [], "posts": [], "recognition": [], "planet": None, "hackorum": None}
    steps = [
        *[(f"pgeu-system {i['base']}", lambda i=i: collect_pgeu(http, roster, i, out)) for i in PGEU_INSTANCES],
        ("Swiss PGDay", lambda: collect_swiss(http, roster, out)),
        ("blogs", lambda: collect_blogs(http, roster, out)),
        ("Planet PostgreSQL", lambda: collect_planet(http, roster, out)),
        ("Hackorum", lambda: collect_hackorum(http, out)),
        ("postgresql.org recognition", lambda: collect_recognition(http, roster, out)),
    ]
    errors = []
    for label, step in steps:
        print(label)
        try:
            step()
        except Exception as e:      # one broken site must not sink the whole page
            errors.append(f"{label}: {e!r}")
            print(f"  ! {label} failed: {e!r}", file=sys.stderr)

    manual_path = ROOT / "community" / "manual.json"
    if manual_path.exists():
        manual = json.loads(manual_path.read_text())
        for e in manual.get("events", []):
            add_event(out, {**e, "source": "manual"})
        for a in manual.get("activities", []):
            out["activities"].append({**a, "evidence": "manual"})
        out["roles"] = manual.get("roles", [])
        out["projects"] = [{**p, **github_stats(http, p.get("repo"))} for p in manual.get("projects", [])]

    # One entry per person/role/event/title (a talk can be listed twice in a schedule).
    uniq = {}
    for a in out["activities"]:
        uniq.setdefault((a["kind"], a["person"], a.get("event"), a.get("title")), a)
    out["activities"] = sorted(uniq.values(), key=lambda a: (a.get("date") or "", a["person"]), reverse=True)
    out["posts"].sort(key=lambda p: p["date"], reverse=True)
    used = {a.get("event") for a in out["activities"]}
    out["events"] = {k: v for k, v in out["events"].items() if k in used}
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "company": COMPANY, "start_year": START_YEAR,
        "people": [p["name"] for p in roster.people],
        **out, "review": roster.review, "errors": errors, "http": http.stats,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", default="community.json", metavar="PATH")
    parser.add_argument("--cache", default=str(ROOT / ".cache" / "http"), metavar="DIR")
    parser.add_argument("--offline", action="store_true", help="use only cached responses")
    args = parser.parse_args()
    data = build(Http(Path(args.cache), offline=args.offline))
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    kinds = {}
    for a in data["activities"]:
        kinds[a["kind"]] = kinds.get(a["kind"], 0) + 1
    print(f"{len(data['activities'])} activities {kinds}, {len(data['posts'])} posts, "
          f"{len(data['events'])} events -> {out} (http {data['http']}, errors {len(data['errors'])})")


if __name__ == "__main__":
    main()
