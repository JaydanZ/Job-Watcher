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
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
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


FETCHERS = {
    "greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
    "workable": fetch_workable, "smartrecruiters": fetch_smartrecruiters,
}
DESCRIPTION_FETCHERS = {
    "greenhouse": fetch_greenhouse_description,
    "workable": fetch_workable_description,
    "smartrecruiters": fetch_smartrecruiters_description,
}


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
        self.city_rx = [re.compile(r"(?<!\w)" + re.escape(c) + r"(?!\w)") for c in self.cities]
        self.code_rx = [re.compile(r",\s*" + re.escape(c) + r"\b") for c in self.codes]

    def segment_ok(self, seg):
        s = seg.lower()
        if any(p in s for p in self.provinces) or any(rx.search(seg) for rx in self.code_rx):
            return True
        if any(rx.search(s) for rx in self.city_rx):
            if "canada" in s or not (self.foreign and self.foreign.search(s)):
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
        segments = re.split(r"\s*[|;]\s*|\s+/\s+|\s+or\s+", location or "")
        return any(self.segment_ok(seg) for seg in segments if seg.strip())


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
    lines = [f"{job['location'] or 'Location not listed'} · posted {human_age(job['posted_at'], now)}"]
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
        return source, slug, FETCHERS[source](slug), None
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
    if len(job["skills_matched"]) < need:
        return False, f"only {len(job['skills_matched'])} skill matches"
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
