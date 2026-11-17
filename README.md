# jobwatch

Checks company job boards every 30 minutes on weekdays and pushes new matching roles to your phone. Runs free on GitHub Actions, with no Claude or AI involved.

It reads each company's public job-board API (Greenhouse, Lever, Ashby, Workday, Oracle) plus Kaiser's career site. Those APIs give exact posting times, so alerts arrive within about 30–60 minutes of a job going live. It does not scrape LinkedIn or Indeed: both forbid scraping and block it quickly.

## Setup (about 10 minutes)

1. **Phone alerts:** install the **ntfy** app (iOS/Android, free). Subscribe to a long random topic name, e.g. `andre-jobs-7f3k9q2xv`. Anyone who knows the name can read it, so keep it random.
2. **Repo:** create a **private** GitHub repo and upload these files: `watch.py`, `config.yaml`, `README.md`, and `.github/workflows/jobwatch.yml`.
3. **Secret:** in the repo, go to Settings → Secrets and variables → Actions → New repository secret. Name it `NTFY_TOPIC` and set the value to your topic name.
4. **First run:** go to Actions → jobwatch → **Run workflow**. Open the run log to see which boards loaded and which failed (lines starting with `!`). Fix or remove the failed slugs in `config.yaml`.

The first run records every open job and alerts only on matching jobs posted in the last 24 hours. After that, you only get alerts for jobs it hasn't seen before.

## Editing

All settings are in `config.yaml`: companies, title keywords, excluded words, local cities and an optional skip list (empty by default). To add a company, open its job board and copy the slug from the URL. The formats are listed at the top of the file.

## How it filters

- **Title:** must match a target keyword and contain none of Senior/Sr/Staff/Principal/Lead/Manager/Director/Architect/Intern/III.
- **Location:** fully remote in the US (or remote with the country unclear, which gets a flag), or on-site in one of the listed OC/IE/North County cities. Remote roles locked to a single non-CA state are dropped.
- **Years:** reads the smallest "N+ years" number in the description. 5 or more is dropped, 3–4 is labeled "warm channel only", and no number found is labeled MAYBE.
- **Flags:** possible Kubernetes requirement, security clearance, contract.

The text checks use keyword matching, so they will be wrong sometimes. Read the posting before acting on an alert.

## Cost

Private repos get 2,000 free Actions minutes a month. This uses about 1 minute per run × about 600 runs a month, so it fits. GitHub sometimes delays scheduled runs by 5–20 minutes at busy times.

## Testing locally

```
pip install requests pyyaml
DRY_RUN=1 python watch.py     # prints matches instead of sending them
```

## Matches page (no notifications)

Each run adds new matches to `docs/matches.json`. The workflow publishes `docs/index.html` with GitHub Pages as a job-board-style page. It has Inbox, Saved, Applied and Hidden tabs, search and filters, and a "New" badge on anything found since your last visit. Your Saved, Applied and Hidden choices are kept in your browser.

One-time setup: go to repo **Settings → Pages → Build and deployment → Source** and select **GitHub Actions**. Then run the workflow once by hand. The page address is `https://<username>.github.io/<repo>/`.

GitHub Pages is free for public repos. Private repos need a paid GitHub plan.

Email and ntfy alerts stay off unless you add their secrets (`SMTP_USER`/`SMTP_PASS` or `NTFY_TOPIC`).
