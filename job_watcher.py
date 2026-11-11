#!/usr/bin/env python3
"""
Job watcher: polls company job boards (Greenhouse, Lever, Ashby) and sends a
push notification for each newly posted role that matches your title, location,
experience-level and skill filters.

Standard library only, no installs needed.

Usage:
    python job_watcher.py              # normal run (notifies + saves state)
    python job_watcher.py --dry-run    # print matches only, no notifications, no state saved
"""
import argparse
import csv
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"
COMPANIES_FILE = ROOT / "companies.json"
SEEN_FILE = ROOT / "seen.json"
LOG_FILE = ROOT / "jobs_found.csv"
USER_AGENT = "personal-job-watcher/1.0"


# ---------------------------------------------------------------- helpers

def get_json(url, timeout=20):
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def parse_time(value):
    """Turn an ISO string or epoch-milliseconds number into an aware datetime."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def html_to_text(raw):
    if not raw:
        return ""
    text = html.unescape(raw)  # Greenhouse double-escapes its HTML
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def pretty_name(slug):
    from urllib.parse import unquote
    return re.sub(r"[-_]+", " ", unquote(slug)).title()


# ---------------------------------------------------------------- fetching

def fetch_greenhouse(slug):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    jobs = []
    for j in data.get("jobs", []):
        jobs.append({
            "id": str(j["id"]),
            "source": "greenhouse", "slug": slug,
            "company": j.get("company_name") or pretty_name(slug),
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            # first_published = when it went live; updated_at changes on edits, so it's not used
            "posted_at": parse_time(j.get("first_published")),
            "description": None,  # fetched later, only for jobs that pass the cheap filters
        })
    return jobs


def fetch_greenhouse_description(slug, job_id):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}")
    return html_to_text(data.get("content", ""))


def fetch_lever(slug):
    data = get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or [cats.get("location", "")]
        loc = " | ".join(l for l in locs if l)
        if j.get("workplaceType") == "remote" and "remote" not in loc.lower():
            loc = f"{loc} (Remote)".strip()
        parts = [j.get("descriptionPlain", "")]
        for section in j.get("lists") or []:
            parts.append(section.get("text", ""))
            parts.append(html_to_text(section.get("content", "")))
        parts.append(j.get("additionalPlain", ""))
        jobs.append({
            "id": j["id"],
            "source": "lever", "slug": slug,
            "company": pretty_name(slug),
            "title": j.get("text", ""),
            "location": loc,
            "url": j.get("hostedUrl", ""),
            "posted_at": parse_time(j.get("createdAt")),  # epoch ms
            "description": " ".join(p for p in parts if p),
        })
    return jobs


def fetch_ashby(slug):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location", "")] + [
            s.get("location", "") for s in (j.get("secondaryLocations") or [])
        ]
        loc = " | ".join(l for l in locs if l)
        if j.get("isRemote") and "remote" not in loc.lower():
            loc = f"{loc} (Remote)".strip()
        jobs.append({
            "id": j["id"],
            "source": "ashby", "slug": slug,
            "company": pretty_name(slug),
            "title": j.get("title", ""),
            "location": loc,
            "url": j.get("jobUrl", ""),
            "posted_at": parse_time(j.get("publishedAt")),
            "description": j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")),
        })
    return jobs


COUNTRY_NAMES = {"ca": "Canada", "us": "United States", "gb": "United Kingdom", "uk": "United Kingdom"}


def join_location(*parts):
    return ", ".join(p for p in parts if p)


def fetch_workable(slug):
    # Workable only gives a posting *date* (no time), so freshness falls back to
    # "not seen on the previous run" (see main()).
    data = get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
    company = data.get("name") or pretty_name(slug)
    jobs = []
    for j in data.get("jobs", []):
        locs = [join_location(l.get("city"), l.get("region"), l.get("country"))
                for l in (j.get("locations") or []) if not l.get("hidden")]
        if not locs:
            locs = [join_location(j.get("city"), j.get("state"), j.get("country"))]
        loc = " | ".join(l for l in locs if l)
        if j.get("telecommuting") and "remote" not in loc.lower():
            loc = f"{loc} (Remote)".strip()
        jobs.append({
            "id": j.get("shortcode") or j.get("code") or j.get("url"),
            "source": "workable", "slug": slug,
            "company": company,
            "title": j.get("title", ""),
            "location": loc,
            "url": j.get("url") or j.get("shortlink") or "",
            "posted_at": None,
            "posted_date": j.get("published_on") or j.get("created_at"),
            "description": None,
        })
    return jobs


_workable_details = {}  # one detailed fetch per company per run


def fetch_workable_description(slug, job_id):
    if slug not in _workable_details:
        _workable_details[slug] = get_json(
            f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
    data = _workable_details[slug]
    for j in data.get("jobs", []):
        if job_id in (j.get("shortcode"), j.get("code"), j.get("url")):
            return html_to_text(" ".join(filter(None, [
                j.get("description"), j.get("requirements"), j.get("benefits")])))
    return ""


def fetch_smartrecruiters(slug):
    jobs, offset = [], 0
    while True:
        data = get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
                        f"?limit=100&offset={offset}")
        page = data.get("content", [])
        for j in page:
            l = j.get("location") or {}
            country = COUNTRY_NAMES.get((l.get("country") or "").lower(), (l.get("country") or "").upper())
            loc = l.get("fullLocation") or join_location(l.get("city"), l.get("region"), country)
            if country and country.lower() not in loc.lower():
                loc = join_location(loc, country)
            if l.get("remote") and "remote" not in loc.lower():
                loc = f"{loc} (Remote)".strip()
            jobs.append({
                "id": str(j["id"]),
                "source": "smartrecruiters", "slug": slug,
                "company": (j.get("company") or {}).get("name") or pretty_name(slug),
                "title": j.get("name", ""),
                "location": loc,
                "url": f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
                "posted_at": parse_time(j.get("releasedDate")),
                "description": None,
            })
        offset += 100
        if not page or offset >= data.get("totalFound", 0) or offset >= 2000:
            break
    return jobs


def fetch_smartrecruiters_description(slug, job_id):
    data = get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{job_id}")
    sections = ((data.get("jobAd") or {}).get("sections") or {}).values()
    return html_to_text(" ".join(s.get("text", "") for s in sections if isinstance(s, dict)))


# ---- Phenom career sites (e.g. careers.manulife.com, careers.opentext.com) -----------
# These have no public API, but each search page embeds its results as JSON
# ("phApp.ddo"). companies.json entries look like "careers.manulife.com/global/en".
# Experimental: if a site's page layout differs, the run log shows an error for it.

BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
# Big career sites (Phenom, Workday) are searched instead of read in full.
# configure() overrides these from config.json.
SEARCH_TERMS = ["developer", "engineer"]
MAX_PAGES = 5
TITLE_OK = None        # set by configure(); lets Workday skip work for irrelevant titles
COMPANY_NAMES = {}     # "workday:td.wd3.myworkdayjobs.com/TD_Bank_Careers" -> "TD Bank"
COMPANY_NAMES_FILE = Path(__file__).resolve().parent / "company_names.json"


def configure(cfg):
    global SEARCH_TERMS, MAX_PAGES, TITLE_OK, COMPANY_NAMES
    SEARCH_TERMS = cfg.get("career_site_search_terms", SEARCH_TERMS)
    MAX_PAGES = cfg.get("career_site_max_pages", MAX_PAGES)
    TITLE_OK = TitleFilter(cfg).ok
    if COMPANY_NAMES_FILE.exists():
        COMPANY_NAMES = json.loads(COMPANY_NAMES_FILE.read_text())


def get_text(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def phenom_ddo(page):
    """Pull the embedded phApp.ddo JSON object out of a Phenom page."""
    i = page.find("phApp.ddo")
    if i < 0:
        raise ValueError("page has no phApp.ddo data (not a Phenom page, or layout changed)")
    start = page.find("{", i)
    obj, _ = json.JSONDecoder().raw_decode(page[start:])
    return obj


def find_key(obj, key):
    """Depth-first search for the first value stored under `key`."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        children = obj.values()
    elif isinstance(obj, list):
        children = obj
    else:
        return None
    for child in children:
        found = find_key(child, key)
        if found is not None:
            return found
    return None


def phenom_parts(spec):
    host, _, locale = spec.partition("/")
    return host, locale.strip("/")


def fetch_phenom(spec):
    host, locale = phenom_parts(spec)
    base = f"https://{host}/{locale}" if locale else f"https://{host}"
    domain = host.split(".")[-2] if host.count(".") >= 1 else host
    jobs, seen_ids = [], set()
    for term in SEARCH_TERMS:
        for page_no in range(MAX_PAGES * 2):  # Phenom pages hold 10 jobs, Workday's 20
            url = f"{base}/search-results?keywords={urllib.parse.quote(term)}&from={page_no * 10}&s=1"
            search = find_key(phenom_ddo(get_text(url)), "eagerLoadRefineSearch") or {}
            page = (search.get("data") or {}).get("jobs") or []
            for j in page:
                job_id = str(j.get("jobId") or j.get("jobSeqNo") or "")
                if not job_id or job_id in seen_ids:
                    continue
                seen_ids.add(job_id)
                locs = j.get("multi_location") or [j.get("location") or
                                                  join_location(j.get("city"), j.get("state"), j.get("country"))]
                posted = str(j.get("postedDate") or j.get("dateCreated") or "")
                # Phenom dates are usually midnight placeholders, so treat them as a date only
                posted_at = parse_time(posted) if posted and "T00:00:00" not in posted else None
                jobs.append({
                    "id": job_id,
                    "source": "phenom", "slug": spec,
                    "company": domain.title(),
                    "title": j.get("title", ""),
                    "location": " | ".join(l for l in locs if l),
                    "url": f"{base}/job/{job_id}",
                    "posted_at": posted_at,
                    "posted_date": posted[:10] or None,
                    "description": None,
                })
            total = search.get("totalHits") or 0
            if not page or (page_no + 1) * 10 >= total:
                break
    return jobs


def fetch_phenom_description(spec, job_id):
    host, locale = phenom_parts(spec)
    base = f"https://{host}/{locale}" if locale else f"https://{host}"
    ddo = phenom_ddo(get_text(f"{base}/job/{job_id}"))
    detail = find_key(ddo, "jobDetail") or {}
    job = ((detail.get("data") or {}).get("job")) or {}
    return html_to_text(job.get("description") or find_key(ddo, "description") or "")


# ---- Workday (e.g. td.wd3.myworkdayjobs.com/TD_Bank_Careers) ------------------------
# Uses the JSON search API behind every myworkdayjobs.com career site.

_workday_details = {}  # (spec, externalPath) -> jobPostingInfo, reused for descriptions


def post_json(url, body, timeout=25):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"User-Agent": BROWSER_UA, "Accept": "application/json",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def workday_parts(spec):
    host, _, site = spec.partition("/")
    return host, host.split(".")[0], site.strip("/")


def workday_posted_date(text, today):
    """'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago'."""
    t = (text or "").lower()
    if "today" in t:
        days = 0
    elif "yesterday" in t:
        days = 1
    else:
        m = re.search(r"(\d+)\+?\s*day", t)
        if not m:
            return None
        days = int(m.group(1)) + (1 if "+" in t else 0)
    return (today - timedelta(days=days)).isoformat()


def workday_detail(spec, path):
    key = (spec, path)
    if key not in _workday_details:
        host, tenant, site = workday_parts(spec)
        data = get_json_ua(f"https://{host}/wday/cxs/{tenant}/{site}{path}")
        _workday_details[key] = data.get("jobPostingInfo") or {}
    return _workday_details[key]


def get_json_ua(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def fetch_workday(spec):
    host, tenant, site = workday_parts(spec)
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    today = datetime.now(timezone.utc).date()
    jobs, seen_paths = [], set()
    for term in SEARCH_TERMS:
        total = None
        for page_no in range(MAX_PAGES):
            data = post_json(api, {"appliedFacets": {}, "limit": 20, "offset": page_no * 20,
                                   "searchText": term})
            if total is None:
                total = data.get("total") or 0  # some sites only report it on page 1
            page = data.get("jobPostings") or []
            for j in page:
                path = j.get("externalPath")
                if not path or path in seen_paths:
                    continue
                seen_paths.add(path)
                title, loc = j.get("title", ""), j.get("locationsText", "")
                if re.fullmatch(r"\d+\s+Locations?", loc.strip(), re.I) and (TITLE_OK is None or TITLE_OK(title)):
                    try:  # "3 Locations": look up which ones, only for titles you'd want
                        info = workday_detail(spec, path)
                        loc = " | ".join(filter(None, [info.get("location")] + (info.get("additionalLocations") or []))) or loc
                    except Exception:
                        pass
                jobs.append({
                    "id": path,
                    "source": "workday", "slug": spec,
                    "company": pretty_name(tenant),
                    "title": title,
                    "location": loc,
                    "url": f"https://{host}/en-US/{site}{path}",
                    "posted_at": None,  # Workday only says "Posted Today", "Posted 3 Days Ago", ...
                    "posted_date": workday_posted_date(j.get("postedOn"), today),
                    "description": None,
                })
            if not page or (page_no + 1) * 20 >= total:
                break
    return jobs


def fetch_workday_description(spec, path):
    return html_to_text(workday_detail(spec, path).get("jobDescription", ""))


FETCHERS = {
    "greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
    "workable": fetch_workable, "smartrecruiters": fetch_smartrecruiters,
    "phenom": fetch_phenom, "workday": fetch_workday,
}
DESCRIPTION_FETCHERS = {
    "greenhouse": fetch_greenhouse_description,
    "workable": fetch_workable_description,
    "smartrecruiters": fetch_smartrecruiters_description,
    "phenom": fetch_phenom_description,
    "workday": fetch_workday_description,
}
# Platforms whose boards can't be guessed from a company name (the finder skips them)
EXPLICIT_ONLY = {"phenom", "workday"}


# ---------------------------------------------------------------- title filter

def normalize(text):
    t = text.lower().replace("-", " ").replace("_", " ").replace("/", " ")
    t = re.sub(r"\bfullstack\b", "full stack", t)
    t = re.sub(r"\bfrontend\b", "front end", t)
    t = re.sub(r"\bbackend\b", "back end", t)
    return re.sub(r"\s+", " ", t)


def word_regex(words):
    words = [normalize(w).strip() for w in words if w.strip()]
    if not words:
        return None
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b")


class TitleFilter:
    def __init__(self, cfg):
        self.focus = word_regex(cfg["title_focus_words"])
        self.roles = word_regex(cfg["title_role_words"])
        self.aliases = word_regex(cfg.get("title_aliases", []))
        self.exclude = word_regex(cfg.get("title_exclude_words", []))

    def ok(self, title):
        t = normalize(title)
        if self.exclude and self.exclude.search(t):
            return False
        if self.aliases and self.aliases.search(t):
            return True
        return bool(self.focus.search(t) and self.roles.search(t))


# ---------------------------------------------------------------- location filter

class LocationFilter:
    """
    A job matches if any of its listed locations is:
      - one of your cities (and not e.g. Cambridge, MA or Waterloo, Iowa),
      - anywhere in your province ("Ontario" or ", ON"),
      - remote in one of your remote regions ("Remote - Canada"), or plain "Canada".
    """

    def __init__(self, cfg):
        loc = cfg.get("location_filter", {})
        self.enabled = loc.get("enabled", True)
        self.cities = [c.lower() for c in loc.get("cities", [])]
        self.provinces = [p.lower() for p in loc.get("province_names", [])]
        self.codes = loc.get("province_codes", [])
        self.remote_regions = [r.lower() for r in loc.get("remote_regions", [])]
        self.remote_anywhere = loc.get("include_remote_with_no_country", False)
        foreign = loc.get("not_my_area_markers", [])
        self.foreign = re.compile(
            r"(?<!\w)(" + "|".join(re.escape(m.lower()) for m in foreign) + r")(?!\w)"
        ) if foreign else None
        # Cities that also exist elsewhere (Cambridge, MA / UK): they only count when the
        # posting also mentions Ontario, Canada or another of your cities.
        self.ambiguous = {c.lower() for c in loc.get("ambiguous_cities", [])}
        self.city_rx = {c: re.compile(r"(?<!\w)" + re.escape(c) + r"(?!\w)") for c in self.cities}
        self.code_rx = [re.compile(r",\s*" + re.escape(c) + r"\b") for c in self.codes]

    def has_local_context(self, location):
        s = location.lower()
        return ("canada" in s or any(p in s for p in self.provinces)
                or any(rx.search(location) for rx in self.code_rx)
                or any(rx.search(s) for c, rx in self.city_rx.items() if c not in self.ambiguous))

    def segment_ok(self, seg, context=True):
        s = seg.lower()
        # "Ontario, CA, United States" or "Waterloo, Iowa, US": somewhere else with the same name
        if self.foreign and self.foreign.search(s) and "canada" not in s:
            return False
        if any(p in s for p in self.provinces) or any(rx.search(seg) for rx in self.code_rx):
            return True
        for city, rx in self.city_rx.items():
            if rx.search(s) and (city not in self.ambiguous or context):
                return True
        bare = re.sub(r"[^a-z ]", " ", s).split()
        if bare == ["canada"]:
            return True
        if "remote" in s or "anywhere" in s:
            if any(r in s for r in self.remote_regions):
                return True
            if self.remote_anywhere and set(bare) <= {"remote", "anywhere", "fully", "global"}:
                return True
        return False

    def ok(self, location):
        if not self.enabled:
            return True
        location = location or ""
        context = self.has_local_context(location)
        segments = re.split(r"\s*[|;]\s*|\s+/\s+|\s+or\s+", location)
        return any(self.segment_ok(seg, context) for seg in segments if seg.strip())


# ---------------------------------------------------------------- resume match

YEARS_RX = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|—|to)\s*\d{1,2}\s*\+?\s*)?(?:years?|yrs?)\b[^.;\n]{0,80}?\bexperience",
    re.I,
)


def required_years(text):
    """First 'N+ years ... experience' phrase in the description, or None."""
    for m in YEARS_RX.finditer(text or ""):
        n = int(m.group(1))
        if 0 < n <= 15:
            return n
    return None


class SkillMatcher:
    def __init__(self, cfg):
        self.skills = []
        for name, aliases in cfg.get("my_skills", {}).items():
            terms = sorted({a.lower() for a in (aliases or [name])}, key=len, reverse=True)
            rx = re.compile(r"(?<![\w.#+])(" + "|".join(re.escape(t) for t in terms) + r")(?![\w#+])", re.I)
            self.skills.append((name, rx))

    def matches(self, text):
        return [name for name, rx in self.skills if rx.search(text or "")]


# ---------------------------------------------------------------- notifying

def human_age(posted_at, now):
    if not posted_at:
        return "just appeared"
    mins = int((now - posted_at).total_seconds() // 60)
    return "just now" if mins < 1 else f"{mins} min ago"


def send_ntfy(server, topic, title, message, click=None, tags=None, priority=3):
    payload = {"topic": topic, "title": title, "message": message, "priority": priority}
    if click:
        payload["click"] = click
    if tags:
        payload["tags"] = tags
    req = urllib.request.Request(
        server.rstrip("/"),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=15).read()


def describe(job, now):
    if job.get("posted_at"):
        when = f"posted {human_age(job['posted_at'], now)}"
    elif job.get("posted_date") == now.date().isoformat():
        when = "posted today"
    elif job.get("posted_date"):
        when = f"posted {job['posted_date']}"
    else:
        when = "just appeared"
    lines = [f"{job['location'] or 'Location not listed'} · {when}"]
    skills = job.get("skills_matched")
    if skills is None:
        lines.append("Couldn't read the description, so skills weren't checked")
    else:
        shown = ", ".join(skills[:8]) + ("…" if len(skills) > 8 else "")
        lines.append(f"Matches {len(skills)} of your skills: {shown}")
    if job.get("years_required"):
        lines.append(f"Asks for {job['years_required']}+ yrs experience")
    return "\n".join(lines)


def notify(jobs, cfg, now):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("NTFY_TOPIC not set, skipping notifications.")
        return
    server = cfg.get("ntfy_server", "https://ntfy.sh")
    cap = cfg.get("max_notifications_per_run", 10)
    strong = cfg.get("resume_match", {}).get("strong_match_skills", 6)
    for job in jobs[:cap]:
        is_strong = len(job.get("skills_matched") or []) >= strong
        try:
            send_ntfy(
                server, topic,
                title=f"{'⭐ ' if is_strong else ''}{job['title']} @ {job['company']}",
                message=describe(job, now),
                click=job["url"], tags=["briefcase"],
                priority=4 if is_strong else 3,
            )
        except Exception as e:  # keep going if one notification fails
            print(f"  ! notification failed for {job['url']}: {e}")
    extra = len(jobs) - cap
    if extra > 0:
        repo_url = None
        if os.environ.get("GITHUB_REPOSITORY"):
            repo_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                        f"{os.environ['GITHUB_REPOSITORY']}/blob/main/jobs_found.csv")
        try:
            send_ntfy(server, topic, title=f"+{extra} more new jobs",
                      message="See jobs_found.csv for the full list.",
                      click=repo_url, tags=["briefcase"])
        except Exception as e:
            print(f"  ! summary notification failed: {e}")


def append_log(jobs, now):
    new_file = not LOG_FILE.exists()
    with LOG_FILE.open("a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["found_at_utc", "posted_at_utc", "company", "title", "location",
                        "skills_matched", "years_required", "url"])
        for j in jobs:
            skills = j.get("skills_matched")
            w.writerow([
                now.strftime("%Y-%m-%d %H:%M"),
                j["posted_at"].strftime("%Y-%m-%d %H:%M") if j["posted_at"] else "",
                j["company"], j["title"], j["location"],
                "" if skills is None else f"{len(skills)}: {', '.join(skills)}",
                j.get("years_required") or "",
                j["url"],
            ])


# ---------------------------------------------------------------- main

def load_json(path, default):
    if path.exists():
        with path.open() as f:
            return json.load(f)
    return default


def fetch_one(source, slug):
    try:
        jobs = FETCHERS[source](slug)
        name = COMPANY_NAMES.get(f"{source}:{slug}")
        if name:
            for j in jobs:
                j["company"] = name
        return source, slug, jobs, None
    except urllib.error.HTTPError as e:
        return source, slug, None, f"HTTP {e.code} (check the slug)"
    except Exception as e:
        return source, slug, None, str(e)


def check_resume_fit(job, cfg, skills):
    """Returns (passes, reason). Fills job['skills_matched'] and job['years_required']."""
    rm = cfg.get("resume_match", {})
    desc = job.get("description")
    if desc is None and job["source"] in DESCRIPTION_FETCHERS:
        try:
            desc = DESCRIPTION_FETCHERS[job["source"]](job["slug"], job["id"])
        except Exception as e:
            print(f"  ! couldn't fetch description for {job['url']}: {e}")
    if not desc:
        job["skills_matched"] = None
        return True, "no description (alerting anyway)"

    job["skills_matched"] = skills.matches(desc)
    job["years_required"] = required_years(desc)
    max_years = rm.get("max_years_required")
    if max_years and job["years_required"] and job["years_required"] > max_years:
        return False, f"asks for {job['years_required']}+ yrs"
    need = rm.get("min_skills_matched", 0)
    junior = word_regex(rm.get("junior_title_words", []))
    if junior and junior.search(normalize(job["title"])):
        # Junior/intermediate postings often describe the work without listing much tech
        need = rm.get("min_skills_matched_junior", need)
    if len(job["skills_matched"]) < need:
        return False, f"only {len(job['skills_matched'])} skill matches"
    # Must mention at least one of your core stack skills (general skills like AWS or
    # Docker alone don't qualify). Entry-level titles are exempt: their postings often
    # don't name a stack at all.
    core = set(rm.get("core_skills", []))
    min_core = rm.get("min_core_skills", 0)
    entry = word_regex(rm.get("entry_level_title_words", []))
    if core and min_core and not (entry and entry.search(normalize(job["title"]))):
        if sum(1 for s in job["skills_matched"] if s in core) < min_core:
            return False, "no core stack skills mentioned"
    return True, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print matches only; no notifications, no state saved")
    args = ap.parse_args()

    cfg = load_json(CONFIG_FILE, None)
    companies = load_json(COMPANIES_FILE, None)
    if cfg is None or companies is None:
        sys.exit("config.json and companies.json are required.")

    configure(cfg)
    titles, places, skills = TitleFilter(cfg), LocationFilter(cfg), SkillMatcher(cfg)
    max_age_min = cfg.get("max_age_minutes", 60)
    seen = load_json(SEEN_FILE, {})  # {"greenhouse:stripe": {"job_id": "first_seen_iso"}}
    now = datetime.now(timezone.utc)

    tasks = [(src, slug) for src, slugs in companies.items() if src in FETCHERS for slug in slugs]
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda t: fetch_one(*t), tasks))

    candidates, failures = [], []
    for source, slug, jobs, err in results:
        key = f"{source}:{slug}"
        if err:
            failures.append(f"{key}: {err}")
            continue  # keep its previous seen-state so nothing re-alerts next run

        prev = seen.get(key)
        first_time = prev is None  # company just added: seed it quietly
        prev = prev or {}
        current = {}
        for job in jobs:
            current[job["id"]] = prev.get(job["id"], now.isoformat(timespec="seconds"))
            if job["id"] in prev:
                continue
            if not (titles.ok(job["title"]) and places.ok(job["location"])):
                continue
            if job["posted_at"]:
                fresh = (now - job["posted_at"]).total_seconds() / 60 <= max_age_min
            else:
                # No timestamp: being unseen since the last run is the freshness signal,
                # as long as any posting date given isn't older than yesterday.
                fresh = not first_time
                d = str(job.get("posted_date") or "")[:10]
                if fresh and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                    fresh = (now.date() - datetime.fromisoformat(d).date()).days <= 1
            if fresh:
                candidates.append(job)
        seen[key] = current  # only keep jobs that are still listed

    matches, skipped = [], []
    for job in candidates:
        passes, reason = check_resume_fit(job, cfg, skills)
        (matches if passes else skipped).append((job, reason))

    print(f"Checked {len(tasks) - len(failures)}/{len(tasks)} boards at {now:%Y-%m-%d %H:%M} UTC")
    for f in failures:
        print(f"  ! {f}")
    print(f"{len(matches)} new matching job(s)")
    for j, _ in sorted(matches, key=lambda m: len(m[0].get("skills_matched") or []), reverse=True):
        sk = j.get("skills_matched")
        print(f"  + {j['title']} @ {j['company']} | {j['location']} | {human_age(j['posted_at'], now)} | "
              f"skills: {'?' if sk is None else len(sk)} | {j['url']}")
    for j, why in skipped:
        print(f"  - skipped {j['title']} @ {j['company']}: {why}")

    matches = [j for j, _ in matches]
    # Best skill matches first, so they're never cut off by the notification cap
    matches.sort(key=lambda j: len(j.get("skills_matched") or []), reverse=True)

    if args.dry_run:
        return
    if matches:
        notify(matches, cfg, now)
        append_log(matches, now)
    with SEEN_FILE.open("w") as f:
        json.dump(seen, f, indent=0, sort_keys=True)


if __name__ == "__main__":
    main()
