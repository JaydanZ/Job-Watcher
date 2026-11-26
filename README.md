# Job watcher

A personal tool that checks company job boards on a schedule and sends a phone
notification when a matching role is posted.

## Setup

1. Install the **ntfy** app and subscribe to a private topic name.
2. Add that topic name as a repository secret called `NTFY_TOPIC`.
3. In **Settings → Actions → General**, give workflows **Read and write** permissions.
4. Run **Job watcher** once from the **Actions** tab. After that, it runs on its schedule.

## Workday checks (on your Windows computer)

Workday refuses requests from GitHub, so Workday companies are checked from your PC.

1. Install Python from python.org and tick **Add python.exe to PATH**.
2. Download this repo (**Code → Download ZIP**) and extract it somewhere permanent.
3. Double-click `windows\setup_workday.bat` and paste your ntfy topic when asked.

It checks every 30 minutes while you're signed in, using the latest settings and
company list from this repo. `windows\run_workday_now.bat` runs a check on demand;
`windows\remove_workday_task.bat` stops it. See `workday_log.txt` for what each
check did.

## Workflows

| Workflow | What it does |
|---|---|
| **Job watcher** | Checks for new postings and sends alerts. Runs on a schedule. |
| **Find companies** | Checks the names in `candidates.txt` and adds relevant companies to `companies.json`. Run manually. |
| **List open jobs** | Writes every currently open match to `open_jobs.md` and `open_jobs.csv`. Run manually. |

## Files

| File | Purpose |
|---|---|
| `config.json` | Search preferences: titles, locations, experience level, skills. |
| `companies.json` | Companies being watched. |
| `candidates.txt` | Companies for the finder to check. Add a name per line. |
| `company_names.json` | Display names for some companies. Written by the finder. |
| `seen.json` | Postings already checked. Safe to delete; it rebuilds quietly. |
| `jobs_found.csv` | Log of every alert sent. |

## Notes

- The first check of a newly added company never sends alerts for its existing
  postings.
- Each run's log lists any companies that couldn't be read, and why postings were
  skipped.
