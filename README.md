# Job watcher

Checks company job boards every 15 minutes and sends a push notification to your
phone for each new software developer / engineer, full stack, front end, or back end
role posted within the last hour.

It reads the public job board feeds that Greenhouse, Lever, and Ashby provide for
the companies in `companies.json`. No API keys, no scraping, and no installs (Python
standard library only).

## Setup (about 10 minutes)

### 1. Get notifications on your phone
1. Install the free **ntfy** app (iOS or Android).
2. Tap **+** and subscribe to a topic with a long random name, e.g.
   `jobs-k3v9x2qp7m`. Anyone who knows the name can read it, so don't use
   something guessable.

### 2. Put the code on GitHub
1. Create a new repository and upload everything in this folder, including the
   `.github` folder.
2. In the repo, go to **Settings → Secrets and variables → Actions → New repository
   secret**. Name it `NTFY_TOPIC` and paste your topic name.
3. Go to **Settings → Actions → General → Workflow permissions** and select
   **Read and write permissions**.

### 3. Start it
Go to the **Actions** tab, open **Job watcher**, and click **Run workflow**.
After that it runs on its own every 15 minutes.

The first run records everything already posted without alerting, except roles
posted in the last hour, so you won't get a flood of old jobs.

**Public or private repo?** Public repos get unlimited Actions minutes. Private
repos have a monthly free allowance, and running every 15 minutes may use it up.
If your repo is private, change the schedule in
`.github/workflows/job-watcher.yml` to `*/30 * * * *`.

## Customizing

**`config.json`**
- `locations`: only alert for jobs whose location contains one of these words,
  e.g. `["toronto", "canada", "remote"]`. Leave it empty to see every location.
- `title_exclude_words`: add `"senior"`, `"staff"`, `"principal"`, or `"intern"` to
  skip those levels.
- `title_focus_words` / `title_role_words`: a title matches when it contains one word
  from each list (e.g. "Back End" + "Engineer"). Add `"web"` to also catch
  "Web Developer".
- `max_age_minutes`: how fresh a posting must be to trigger an alert (default 60).

**`companies.json`**: add companies under the platform they use. To find a
company's slug, open one of its job postings and check the URL:

| URL looks like | Add under | Slug |
|---|---|---|
| `boards.greenhouse.io/figma/...` or `job-boards.greenhouse.io/figma` | `greenhouse` | `figma` |
| `jobs.lever.co/spotify/...` | `lever` | `spotify` |
| `jobs.ashbyhq.com/ramp/...` | `ashby` | `ramp` |

If a company uses its own careers domain, its postings often still link to one of
these. A wrong slug just shows up as an error line in the run log and is skipped.

## Files the watcher writes
- `seen.json`: job IDs already checked, so nothing alerts twice.
- `jobs_found.csv`: every job it alerted you about, a running log you can use as
  an application tracker.

## Running locally
```
NTFY_TOPIC=your-topic python3 job_watcher.py      # normal run
python3 job_watcher.py --dry-run                  # just print matches
```

## Limitations
- Only covers companies in your list that use Greenhouse, Lever, or Ashby. Workday,
  iCIMS, LinkedIn, and Indeed aren't included.
- GitHub sometimes starts scheduled runs late, especially at the top of the hour,
  so alerts can arrive a bit later than 15 minutes after posting.
