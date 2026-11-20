Compare Jira story-point estimates with PI capacity hours from Google Sheets.

## Setup

1. Create a Jira Personal Access Token: https://jira.trimble.tools → Profile → Personal Access Tokens
2. Copy `.env.example` to `.env` and paste the token into `JIRA_PAT`
3. Put the Google service account JSON at `credentials/service-account.json` (already gitignored). Share the sheet with `jira-capacity-comparison@refined-outlet-228210.iam.gserviceaccount.com`.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python compare_capacity.py
```

If Google OAuth is blocked, export the **capacity** tab to CSV and run:

```powershell
python compare_capacity.py --capacity-csv .\capacity.csv
```

When you run it, the script asks for:

1. **Google Sheet link** (full URL or spreadsheet ID)
2. **Fix version** (example: `2026.05.V1.0`)
3. **Username column** — letter or header (`D` or `Name`)
4. **Capacity column** — letter or header (`R`)

```powershell
python compare_capacity.py
python compare_capacity.py --sheet-url "https://docs.google.com/spreadsheets/d/1qrOqHYklb0hYD-xoIXj7P9gNnA7wsuo2jnaQhmJaFCU/edit?gid=0" --fix-version 2026.05.V1.0 --name-column D --capacity-column R
```

Reports land in `output/`. Occupied % is `story points / capacity hours * 100` (1 point = 1 hour).

## Nightly IN Verification → Confluence

`update_in_verification.py` reads GSIS board tickets in **IN Verification** and updates [Tickets blocked by stakeholder](https://confluence.trimble.tools/spaces/GS/pages/1261831310/Tickets+blocked+by+stakeholder).

Columns: Ticket, Title (summary), Waiting on (current assignee), Since (last move into IN Verification), Days, Reason (latest comment), Status.

```powershell
python update_in_verification.py --dry-run
python update_in_verification.py
.\install_nightly_task.ps1 11:30
```

On Windows this registers Task Scheduler job **GSIS IN Verification Confluence Sync** for Monday–Friday at 11:30 (not a Unix crontab).

## Weekday Blocker → Confluence

`update_blocked.py` reads GSIS board tickets in **Blocker** (the board column for blocked work) and updates [Tickets in 'Blocked' state](https://confluence.trimble.tools/spaces/GS/pages/1303283178/Tickets+in+Blocked+state).

Columns match the IN Verification page: Ticket, Title, Waiting on (current assignee), Since, Days, Reason (latest comment), Status.

```powershell
python update_blocked.py --dry-run
python update_blocked.py
.\install_nightly_task.ps1 11:30
```

`install_nightly_task.ps1` also registers **GSIS Blocked Confluence Sync** for Monday–Friday at 11:30.
