# Job watcher
 
Checks company job boards every 15 minutes and sends a push notification to your
phone for each new software developer / engineer, full stack, front end, or back end
role posted within the last hour that:
 
- is in Toronto, Kitchener, Waterloo, Cambridge, Guelph, Mississauga, anywhere else in
  Ontario, or remote in Canada,
- is junior or intermediate level (senior/staff/lead/intern titles are skipped),
- asks for 4 years of experience or less, and
- mentions at least 3 skills from your resume.
Each notification shows which of your skills the posting mentions and how many years
it asks for. Postings that mention 6 or more of your skills get a ⭐ and a
higher-priority alert.
 
It reads the public job board feeds that Greenhouse, Lever, Ashby, Workable, and
SmartRecruiters provide for the companies in `companies.json`. No API keys, no scraping, and no installs (Python
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
- `location_filter`: `cities` are matched anywhere in the location; any location
  labelled "Ontario" or ", ON" also matches. `remote_regions` covers "Remote - Canada"
  style postings. Set `include_remote_with_no_country` to `true` to also get postings
  that just say "Remote" (these are often US-only). `not_my_area_markers` stops
  look-alikes such as Cambridge, MA or Waterloo, Iowa.
- `resume_match`:
  - `min_skills_matched`: how many of your skills a posting must mention (default 3).
  - `max_years_required`: skip postings asking for more years than this (default 4).
  - `strong_match_skills`: skill count that earns a ⭐ alert (default 6).
- `my_skills`: the skills from your resume, each with the spellings to look for.
  Add new skills here as you learn them.
- `title_exclude_words`: remove `"senior"` and `"sr"` if you want to see senior roles
  too (the years filter still applies).
- `title_focus_words` / `title_role_words`: a title matches when it contains one word
  from each list (e.g. "Back End" + "Engineer").
- `max_age_minutes`: how fresh a posting must be to trigger an alert (default 60).
The GitHub Actions log for each run lists the jobs it skipped and why, e.g.
"asks for 7+ yrs" or "only 1 skill matches". Check it now and then to tune these
numbers.
 
**`companies.json`**: add companies under the platform they use. To find a
company's slug, open one of its job postings and check the URL:
 
| URL looks like | Add under | Slug |
|---|---|---|
| `boards.greenhouse.io/figma/...` or `job-boards.greenhouse.io/figma` | `greenhouse` | `figma` |
| `jobs.lever.co/spotify/...` | `lever` | `spotify` |
| `jobs.ashbyhq.com/ramp/...` | `ashby` | `ramp` |
| `apply.workable.com/tribalscale/...` | `workable` | `tribalscale` |
| `jobs.smartrecruiters.com/Visa/...` | `smartrecruiters` | `Visa` |
 
If a company uses its own careers domain, its postings often still link to one of
these. A wrong slug just shows up as an error line in the run log and is skipped.
 
## One-time list of everything open right now
 
The watcher only alerts on new postings. For a full list of every open role that
matches your search, whatever its posting date, go to **Actions → List open jobs →
Run workflow**. In a few minutes it creates:
 
- `open_jobs.md`: open it on GitHub for a clickable list. "Good fits" (pass your
  resume rules) come first, newest first, with the skills each posting mentions.
  Roles your rules filtered out are listed below with the reason.
- `open_jobs.csv`: the same data to open in Excel or Google Sheets.
Run it again whenever you want a fresh list.
 
## Finding more companies automatically
 
`candidates.txt` has about 3,100 entries:
- ~800 Ontario employers by name: tech companies, startups, agencies, consultancies,
  and non-tech companies with developer teams. You don't need to know which job
  platform each one uses.
- ~2,300 exact job boards taken from the public SimplifyJobs listings on GitHub.
  About 150 of them, marked with `!`, have posted Ontario or remote-Canada roles
  before, so they're added even if nothing is posted in your area today.
The finder takes 15–30 minutes with this many entries.
 
Go to **Actions → Find companies → Run workflow**. It takes about 5–10 minutes. The
finder tries each name on all five platforms and adds every company that **currently
has postings in your area** to `companies.json`. It then writes `finder_report.md`,
which is also shown on the run's summary page, with what it found and how many dev
roles each company has open.
 
- Add names to `candidates.txt` anytime (just the company name, one per line) and
  run it again. Companies already in `companies.json` are skipped. An exact board
  like `lever:roserocket` also works; end it with `!` to always add it.
- Skim the report's *Board name* column. Short names like "Drop" or "Muse" can
  match a different company. Delete any wrong ones from `companies.json`.
- Big banks, governments, and many large retailers use Workday or their own sites,
  which the finder can't read, so expect most of those names to come up empty.
- Newly added companies are recorded quietly on the next watcher run, so you won't
  get alerts for their existing postings.
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
- Only covers companies in your list that use Greenhouse, Lever, Ashby, Workable, or
  SmartRecruiters. Workday, iCIMS, Taleo, LinkedIn, and Indeed aren't included, so
  large Ontario employers such as Shopify, D2L, the banks, and Google aren't covered.
- Workable only gives a posting date, not a time. For those companies, "new" means
  "wasn't there 15 minutes ago and was posted today or yesterday".
- Skill matching looks for keywords, so it can't tell "React required" from
  "React is a plus". Treat the count as a guide.
- The years filter reads the first "N+ years ... experience" phrase in a posting.
  Phrasing like "five years" in words is missed, and that posting won't be skipped.
- GitHub sometimes starts scheduled runs late, especially at the top of the hour,
  so alerts can arrive a bit later than 15 minutes after posting.
