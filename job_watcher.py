#!/usr/bin/env python3
"""
Job watcher: polls company job boards (Greenhouse, Lever, Ashby) and sends a
push notification for each newly posted role that matches your filters.

Standard library only, no installs needed.

Usage:
    python job_watcher.py              # normal run (notifies + saves state)
    python job_watcher.py --dry-run    # print matches only, no notifications, no state saved
"""
import argparse
import csv
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


# ---------------------------------------------------------------- fetching

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


def fetch_greenhouse(slug):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    jobs = []
    for j in data.get("jobs", []):
        jobs.append({
            "id": str(j["id"]),
            "company": j.get("company_name") or slug,
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            # first_published = when it went live; updated_at changes on edits, so it's not used
            "posted_at": parse_time(j.get("first_published")),
        })
    return jobs


def fetch_lever(slug):
    data = get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or [cats.get("location", "")]
        loc = ", ".join(l for l in locs if l)
        if j.get("workplaceType") == "remote" and "remote" not in loc.lower():
            loc = f"{loc} (Remote)".strip()
        jobs.append({
            "id": j["id"],
            "company": slug,
            "title": j.get("text", ""),
            "location": loc,
            "url": j.get("hostedUrl", ""),
            "posted_at": parse_time(j.get("createdAt")),  # epoch ms
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
        loc = ", ".join(l for l in locs if l)
        if j.get("isRemote") and "remote" not in loc.lower():
            loc = f"{loc} (Remote)".strip()
        jobs.append({
            "id": j["id"],
            "company": slug,
            "title": j.get("title", ""),
            "location": loc,
            "url": j.get("jobUrl", ""),
            "posted_at": parse_time(j.get("publishedAt")),
        })
    return jobs


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


# ---------------------------------------------------------------- filtering

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


class Filters:
    def __init__(self, cfg):
        self.focus = word_regex(cfg["title_focus_words"])
        self.roles = word_regex(cfg["title_role_words"])
        self.aliases = word_regex(cfg.get("title_aliases", []))
        self.exclude = word_regex(cfg.get("title_exclude_words", []))
        self.locations = [l.lower() for l in cfg.get("locations", []) if l.strip()]

    def title_ok(self, title):
        t = normalize(title)
        if self.exclude and self.exclude.search(t):
            return False
        if self.aliases and self.aliases.search(t):
            return True
        return bool(self.focus.search(t) and self.roles.search(t))

    def location_ok(self, location):
        if not self.locations:
            return True  # no location filter set
        loc = location.lower()
        return any(l in loc for l in self.locations)


# ---------------------------------------------------------------- notifying

def human_age(posted_at, now):
    if not posted_at:
        return "just appeared"
    mins = int((now - posted_at).total_seconds() // 60)
    return "just now" if mins < 1 else f"{mins} min ago"


def send_ntfy(server, topic, title, message, click=None, tags=None):
    payload = {"topic": topic, "title": title, "message": message}
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


def notify(jobs, cfg, now):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("NTFY_TOPIC not set, skipping notifications.")
        return
    server = cfg.get("ntfy_server", "https://ntfy.sh")
    cap = cfg.get("max_notifications_per_run", 10)
    for job in jobs[:cap]:
        try:
            send_ntfy(
                server, topic,
                title=f"{job['title']} @ {job['company']}",
                message=f"{job['location'] or 'Location not listed'} · posted {human_age(job['posted_at'], now)}",
                click=job["url"], tags=["briefcase"],
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
            w.writerow(["found_at_utc", "posted_at_utc", "company", "title", "location", "url"])
        for j in jobs:
            w.writerow([
                now.strftime("%Y-%m-%d %H:%M"),
                j["posted_at"].strftime("%Y-%m-%d %H:%M") if j["posted_at"] else "",
                j["company"], j["title"], j["location"], j["url"],
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print matches only; no notifications, no state saved")
    args = ap.parse_args()

    cfg = load_json(CONFIG_FILE, None)
    companies = load_json(COMPANIES_FILE, None)
    if cfg is None or companies is None:
        sys.exit("config.json and companies.json are required.")

    filters = Filters(cfg)
    max_age_min = cfg.get("max_age_minutes", 60)
    seen = load_json(SEEN_FILE, {})  # {"greenhouse:stripe": {"job_id": "first_seen_iso"}}
    now = datetime.now(timezone.utc)

    tasks = [(src, slug) for src, slugs in companies.items() if src in FETCHERS for slug in slugs]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda t: fetch_one(*t), tasks))

    matches, failures = [], []
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
            if not (filters.title_ok(job["title"]) and filters.location_ok(job["location"])):
                continue
            if job["posted_at"]:
                age_min = (now - job["posted_at"]).total_seconds() / 60
                fresh = age_min <= max_age_min
            else:
                # No timestamp: being unseen since the last run is the freshness signal.
                fresh = not first_time
            if fresh:
                matches.append(job)
        seen[key] = current  # only keep jobs that are still listed

    matches.sort(key=lambda j: j["posted_at"] or now, reverse=True)

    print(f"Checked {len(tasks) - len(failures)}/{len(tasks)} boards at {now:%Y-%m-%d %H:%M} UTC")
    for f in failures:
        print(f"  ! {f}")
    print(f"{len(matches)} new matching job(s)")
    for j in matches:
        print(f"  - {j['title']} @ {j['company']} | {j['location']} | "
              f"{human_age(j['posted_at'], now)} | {j['url']}")

    if args.dry_run:
        return
    if matches:
        notify(matches, cfg, now)
        append_log(matches, now)
    with SEEN_FILE.open("w") as f:
        json.dump(seen, f, indent=0, sort_keys=True)


if __name__ == "__main__":
    main()
