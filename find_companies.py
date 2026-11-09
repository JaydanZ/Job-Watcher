#!/usr/bin/env python3
"""
Company finder: tries every name in candidates.txt on Greenhouse, Lever, Ashby,
Workable and SmartRecruiters. Any company that currently has job postings in your
area (as defined by location_filter in config.json) gets added to companies.json,
so the watcher starts checking it.

Usage:
    python find_companies.py              # search, update companies.json, write finder_report.md
    python find_companies.py --dry-run    # search and report only

Lines in candidates.txt can be:
    Company Name         -> several likely slugs are tried on every platform
    lever:roserocket     -> tests exactly that board; added if it has jobs in your area now
    lever:roserocket!    -> tests exactly that board; added whenever it exists, even with
                            nothing in your area today (for companies known to hire here)
    # comment            -> ignored
"""
import argparse
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import job_watcher as jw

ROOT = Path(__file__).resolve().parent
CANDIDATES_FILE = ROOT / "candidates.txt"
REPORT_FILE = ROOT / "finder_report.md"

FILLER = {"inc", "ltd", "llc", "corp", "corporation", "co", "company", "technologies",
          "technology", "tech", "labs", "software", "group", "the", "limited", "systems",
          "solutions", "digital", "io", "hq", "app"}


def slug_variants(name):
    n = re.sub(r"\(.*?\)", " ", name.lower().replace("&", " and "))
    words = re.findall(r"[a-z0-9]+", n)
    if not words:
        return []
    core = [w for w in words if w not in FILLER] or words
    out = []
    for v in ("".join(words), "-".join(words), "".join(core), "-".join(core)):
        if v and v not in out:
            out.append(v)
    return out


def read_candidates():
    tasks = []  # (label, platform, slug)
    for raw in CANDIDATES_FILE.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.fullmatch(r"(\w+):([\w.%\-]+)(!?)", line)
        if m and m.group(1) in jw.FETCHERS:
            label = raw.split("#", 1)[1].strip() if "#" in raw else line
            tasks.append((label + m.group(3), m.group(1), m.group(2)))
            continue
        for slug in slug_variants(line):
            for platform in jw.FETCHERS:
                tasks.append((line, platform, slug))
    return tasks


def probe(task, titles, places):
    label, platform, slug = task
    try:
        jobs = jw.FETCHERS[platform](slug)
    except Exception:
        return None  # 404 or unreachable: not on this platform under this slug
    if not jobs:
        return None
    local = [j for j in jobs if places.ok(j["location"])]
    dev = [j for j in local if titles.ok(j["title"])]
    return {
        "label": label, "platform": platform, "slug": slug,
        "board_name": jobs[0]["company"], "total": len(jobs),
        "local": len(local), "dev": len(dev),
        "dev_titles": [j["title"] for j in dev[:3]],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="don't modify companies.json")
    ap.add_argument("--min-local-jobs", type=int, default=1,
                    help="postings in your area a company needs right now to be added")
    args = ap.parse_args()

    cfg = jw.load_json(jw.CONFIG_FILE, None)
    companies = jw.load_json(jw.COMPANIES_FILE, {})
    titles, places = jw.TitleFilter(cfg), jw.LocationFilter(cfg)
    already = {(p, s.lower()) for p, slugs in companies.items() for s in slugs}

    tasks = [t for t in read_candidates() if (t[1], t[2].lower()) not in already]
    tasks = list(dict.fromkeys(tasks))
    print(f"Trying {len(tasks)} name/platform combinations...")
    with ThreadPoolExecutor(max_workers=24) as pool:
        found = [r for r in pool.map(lambda t: probe(t, titles, places), tasks) if r]

    # Keep the best board per (name, platform), then drop boards found via two names.
    best = {}
    for r in found:
        key = (r["label"], r["platform"])
        if key not in best or (r["local"], r["total"]) > (best[key]["local"], best[key]["total"]):
            best[key] = r
    unique = {}
    for r in best.values():
        key = (r["platform"], r["slug"].lower())
        if key not in unique or (r["label"].endswith("!") and not unique[key]["label"].endswith("!")):
            unique[key] = r
    results = sorted(unique.values(), key=lambda r: (r["dev"], r["local"]), reverse=True)

    keep = lambda r: r["local"] >= args.min_local_jobs or r["label"].endswith("!")
    added = [r for r in results if keep(r)]
    not_local = [r for r in results if not keep(r)]

    lines = [
        "# Company finder report", "",
        f"Tried {len(tasks)} name/platform combinations. Found {len(results)} job boards; "
        f"**{len(added)} were added** to companies.json (postings in your area now, or marked "
        "with ! as known to hire in Ontario).", "",
        "Check the *Board name* column: a short or common name can match a different company. "
        "Remove any wrong ones from companies.json.", "",
        "## Added", "",
        "| Searched for | Board | Board name | Jobs total | In your area | Dev roles in your area now |",
        "|---|---|---|---|---|---|",
    ]
    for r in added:
        eg = f" ({'; '.join(r['dev_titles'])})" if r["dev_titles"] else ""
        lines.append(f"| {r['label']} | {r['platform']}:{r['slug']} | {r['board_name']} | "
                     f"{r['total']} | {r['local']} | {r['dev']}{eg} |")
    lines += ["", "## Found, but nothing in your area right now (not added)", "",
              "| Searched for | Board | Board name | Jobs total |", "|---|---|---|---|"]
    for r in not_local:
        lines.append(f"| {r['label']} | {r['platform']}:{r['slug']} | {r['board_name']} | {r['total']} |")
    report = "\n".join(lines) + "\n"
    print(report)

    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(report)
    if args.dry_run:
        return
    REPORT_FILE.write_text(report)
    for r in added:
        companies.setdefault(r["platform"], [])
        if r["slug"] not in companies[r["platform"]]:
            companies[r["platform"]].append(r["slug"])
    with jw.COMPANIES_FILE.open("w") as f:
        json.dump(companies, f, indent=2)
        f.write("\n")


if __name__ == "__main__":
    main()
