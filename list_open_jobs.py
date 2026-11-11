#!/usr/bin/env python3
"""
One-time list of every currently open job (any posting date) across companies.json
that matches your title and location filters, checked against your resume.

Writes:
    open_jobs.md   - readable list with clickable links (open it on GitHub)
    open_jobs.csv  - same data for Excel / Google Sheets

"Good fit" = passes the resume_match rules in config.json (enough of your skills,
not too many years required). Other title/location matches are listed separately.

Usage:
    python list_open_jobs.py
"""
import csv
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import job_watcher as jw

ROOT = Path(__file__).resolve().parent
MD_FILE = ROOT / "open_jobs.md"
CSV_FILE = ROOT / "open_jobs.csv"


def posted_date(job):
    if job.get("posted_at"):
        return job["posted_at"].date()
    d = str(job.get("posted_date") or "")[:10]
    try:
        return datetime.fromisoformat(d).date()
    except ValueError:
        return None


def md_escape(text):
    return str(text).replace("|", "/").replace("\n", " ")


def main():
    cfg = jw.load_json(jw.CONFIG_FILE, None)
    companies = jw.load_json(jw.COMPANIES_FILE, {})
    jw.configure(cfg)
    titles, places, skills = jw.TitleFilter(cfg), jw.LocationFilter(cfg), jw.SkillMatcher(cfg)
    now = datetime.now(timezone.utc)
    today = now.date()

    tasks = [(src, slug) for src, slugs in companies.items() if src in jw.FETCHERS for slug in slugs]
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda t: jw.fetch_one(*t), tasks))
    failures = [f"{s}:{g}: {e}" for s, g, _, e in results if e]

    candidates, seen_urls = [], set()
    for _, _, jobs, err in results:
        for job in jobs or []:
            if job["url"] in seen_urls:
                continue
            if titles.ok(job["title"]) and places.ok(job["location"]):
                seen_urls.add(job["url"])
                candidates.append(job)
    print(f"Checked {len(tasks) - len(failures)}/{len(tasks)} boards; "
          f"{len(candidates)} open jobs match your titles and locations. Reading descriptions...")

    def evaluate(job):
        passes, reason = jw.check_resume_fit(job, cfg, skills)
        job["fit"], job["reason"], job["date"] = passes, reason, posted_date(job)
        return job

    # Hide very old postings: they're usually evergreen listings or forgotten boards
    max_days = cfg.get("max_posting_age_days")
    hidden_old = 0
    if max_days:
        kept = []
        for job in candidates:
            d = posted_date(job)
            if d and (today - d).days > max_days:
                hidden_old += 1
            else:
                kept.append(job)
        candidates = kept

    with ThreadPoolExecutor(max_workers=16) as pool:
        jobs = list(pool.map(evaluate, candidates))

    def sort_key(j):
        return (j["date"] or datetime(1970, 1, 1).date(), len(j.get("skills_matched") or []))
    good = sorted([j for j in jobs if j["fit"]], key=sort_key, reverse=True)
    other = sorted([j for j in jobs if not j["fit"]], key=sort_key, reverse=True)

    def age(j):
        if not j["date"]:
            return "unknown"
        days = (today - j["date"]).days
        return "today" if days <= 0 else "1 day ago" if days == 1 else f"{days} days ago"

    def skills_text(j):
        s = j.get("skills_matched")
        return "?" if s is None else f"{len(s)}: {', '.join(s)}"

    # ---- CSV
    with CSV_FILE.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["good_fit", "posted", "posted_ago", "company", "title", "location",
                    "skills_matched", "years_required", "why_not_a_fit", "url"])
        for j in good + other:
            w.writerow(["yes" if j["fit"] else "no", j["date"] or "", age(j), j["company"], j["title"],
                        j["location"], skills_text(j), j.get("years_required") or "",
                        "" if j["fit"] else j["reason"], j["url"]])

    # ---- Markdown
    lines = [
        "# Open jobs that match your search", "",
        f"Generated {now:%Y-%m-%d %H:%M} UTC from {len(tasks) - len(failures)} company job boards. "
        f"**{len(good)} good fits** and {len(other)} other title/location matches. "
        "Newest first. \"Posted\" is the date the company's job board reports; "
        "reposted jobs can look newer than they are."
        + (f" {hidden_old} postings older than {max_days} days were hidden "
           "(change `max_posting_age_days` in config.json)." if hidden_old else ""), "",
        "## Good fits", "",
        "| Posted | Company | Role | Location | Your skills it mentions | Yrs asked |",
        "|---|---|---|---|---|---|",
    ]
    for j in good:
        s = j.get("skills_matched")
        sk = "couldn't read description" if s is None else f"**{len(s)}**: {', '.join(s[:8])}{'…' if len(s) > 8 else ''}"
        lines.append(f"| {age(j)} | {md_escape(j['company'])} | [{md_escape(j['title'])}]({j['url']}) | "
                     f"{md_escape(j['location'])} | {sk} | {j.get('years_required') or '–'} |")
    lines += ["", "## Other matches (filtered out by your resume rules)", "",
              "| Posted | Company | Role | Location | Why it was filtered |", "|---|---|---|---|---|"]
    for j in other:
        lines.append(f"| {age(j)} | {md_escape(j['company'])} | [{md_escape(j['title'])}]({j['url']}) | "
                     f"{md_escape(j['location'])} | {md_escape(j['reason'])} |")
    if failures:
        lines += ["", "## Boards that couldn't be read", ""] + [f"- {f}" for f in failures]
    report = "\n".join(lines) + "\n"
    MD_FILE.write_text(report)

    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(report[:900_000])  # GitHub caps the summary size
    print(f"{len(good)} good fits, {len(other)} other matches. Wrote open_jobs.md and open_jobs.csv.")


if __name__ == "__main__":
    main()
