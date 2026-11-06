#!/usr/bin/env python3
"""Compare Jira story-point estimates with Google Sheet PI capacity hours."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import truststore

truststore.inject_into_ssl()

import requests
import urllib3
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

TEAM_MEMBERS = [
    "Abinesh M",
    "Anudeep Mannepalli EXT",
    "Aravind Sakthivel",
    "Dharaneedharan GK",
    "Jeyalakshmi Chandrasekaran",
    "Keerthana Samynathan",
    "Leafen Sandy M",
    "Lovedeep Bains EXT",
    "Maheshbabu Pamula",
    "Nagaraju M EXT",
    "Pranesh C",
    "Redlapalli Gouthami",
    "Reshma Sefali A",
    "Saraswathy Annadurai",
    "Sneha G",
    "Srinidhi Hariharan",
    "Thejaswi S",
]

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

HEADER_NAMES = {"name", "names", "member", "resource", "engineer", "assignee"}


@dataclass
class Ticket:
    key: str
    summary: str
    assignee: str | None
    story_points: float
    status: str
    issue_type: str


@dataclass
class MemberStats:
    name: str
    story_points: float = 0.0
    ticket_count: int = 0
    tickets_with_points: int = 0
    capacity_hours: float | None = None
    sheet_name: str | None = None
    tickets: list[Ticket] = field(default_factory=list)

    @property
    def remaining_hours(self) -> float | None:
        if self.capacity_hours is None:
            return None
        return round(self.capacity_hours - self.story_points, 2)

    @property
    def occupied_pct(self) -> float | None:
        if self.capacity_hours is None or self.capacity_hours == 0:
            return None
        return round((self.story_points / self.capacity_hours) * 100, 1)


def load_env(project_root: Path) -> None:
    load_dotenv(project_root / ".env")


def env_str(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None or value.strip() == "":
        raise SystemExit(f"Missing required setting: {name}")
    return value.strip()


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y"}


def col_letter_to_index(letter: str) -> int:
    letter = letter.strip().upper()
    if not re.fullmatch(r"[A-Z]+", letter):
        raise SystemExit(f"Invalid column letter: {letter}")
    index = 0
    for char in letter:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def index_to_col_letter(index: int) -> str:
    n = index + 1
    letters = ""
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def normalize_column_spec(spec: str) -> str:
    cleaned = spec.strip()
    cleaned = re.sub(r"^(column|col|row)\s+", "", cleaned, flags=re.I)
    return cleaned.strip()


def resolve_column_spec(spec: str, rows: list[list[str]]) -> tuple[str, int]:
    """Accept a column letter (D, R) or a header/label from the sheet."""
    cleaned = normalize_column_spec(spec)
    if not cleaned:
        raise SystemExit("Username/capacity column cannot be empty.")
    if re.fullmatch(r"[A-Za-z]{1,2}", cleaned):
        letter = cleaned.upper()
        return letter, col_letter_to_index(letter)

    needle = normalize_name(cleaned)
    for row in rows[:15]:
        for idx, cell in enumerate(row):
            if normalize_name(cell or "") == needle:
                return index_to_col_letter(idx), idx
    raise SystemExit(
        f'Could not find Google Sheet column "{spec}". '
        "Enter a column letter (D, R) or a header name from the sheet."
    )


def parse_sheet_link(link: str) -> tuple[str, int | None]:
    """Return (spreadsheet_id, optional gid) from a URL or raw ID."""
    cleaned = link.strip()
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", cleaned)
    if match:
        gid_match = re.search(r"[?&#]gid=(\d+)", cleaned)
        gid = int(gid_match.group(1)) if gid_match else None
        return match.group(1), gid
    if re.fullmatch(r"[a-zA-Z0-9-_]{20,}", cleaned):
        return cleaned, None
    raise SystemExit(
        "Could not parse the Google Sheet link. Paste the full docs.google.com URL "
        "or the spreadsheet ID."
    )


def default_sheet_url() -> str:
    explicit = os.getenv("GOOGLE_SHEET_URL", "").strip()
    if explicit:
        return explicit
    spreadsheet_id = os.getenv("SPREADSHEET_ID", "").strip()
    if spreadsheet_id:
        return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
    return ""
    suffix = f" [{default}]" if default else ""
    entered = input(f"{label}{suffix}: ").strip()
    if entered:
        return entered
    if default:
        return default
    raise SystemExit(f"{label} is required.")


def resolve_run_input(cli_value: str | None, label: str, default: str) -> str:
    if cli_value and cli_value.strip():
        return cli_value.strip()
    if sys.stdin.isatty():
        return prompt_value(label, default)
    if default:
        return default
    raise SystemExit(f"{label} is required. Pass it as a flag or run interactively.")


def normalize_name(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", name).strip().casefold()
    cleaned = re.sub(r"\s+ext$", "", cleaned)
    return cleaned


def parse_hours(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group()) if match else None


def story_points_value(raw: Any) -> float:
    if raw is None or raw == "":
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def build_session(verify_ssl: bool) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.verify = verify_ssl
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return session


def jira_get(
    session: requests.Session,
    base_url: str,
    pat: str,
    path: str,
    params: dict[str, Any] | None = None,
) -> Any:
    url = f"{base_url.rstrip('/')}{path}"
    response = session.get(
        url,
        headers={"Authorization": f"Bearer {pat}", "Accept": "application/json"},
        params=params,
        timeout=60,
    )
    if response.status_code >= 400:
        raise SystemExit(
            f"Jira request failed ({response.status_code}) {url}: {response.text[:500]}"
        )
    return response.json()


def find_board_id(session: requests.Session, base_url: str, pat: str, board_name: str) -> int:
    start_at = 0
    while True:
        data = jira_get(
            session,
            base_url,
            pat,
            "/rest/agile/1.0/board",
            params={"name": board_name, "maxResults": 50, "startAt": start_at},
        )
        for board in data.get("values") or []:
            if board.get("name", "").strip().casefold() == board_name.strip().casefold():
                return int(board["id"])
        if data.get("isLast", True):
            break
        start_at += data.get("maxResults", 50)
    raise SystemExit(f'Could not find Jira board named "{board_name}"')


def resolve_story_points_field(
    session: requests.Session, base_url: str, pat: str, configured: str
) -> str:
    if configured.startswith("customfield_"):
        return configured
    fields = jira_get(session, base_url, pat, "/rest/api/2/field")
    for item in fields:
        if item.get("name") == "Story Points":
            return item["id"]
    raise SystemExit("Could not find a Jira field named 'Story Points'")


def fetch_jira_tickets(
    session: requests.Session,
    base_url: str,
    pat: str,
    board_id: int,
    fix_version: str,
    story_points_field: str,
) -> list[Ticket]:
    jql = f'fixVersion = "{fix_version}" AND issuetype not in subTaskIssueTypes()'
    fields = ",".join(
        ["summary", "assignee", story_points_field, "status", "issuetype"]
    )
    tickets: list[Ticket] = []
    start_at = 0
    page_size = 100
    while True:
        data = jira_get(
            session,
            base_url,
            pat,
            f"/rest/agile/1.0/board/{board_id}/issue",
            params={
                "jql": jql,
                "fields": fields,
                "maxResults": page_size,
                "startAt": start_at,
            },
        )
        issues = data.get("issues") or []
        for issue in issues:
            fields_data = issue.get("fields") or {}
            assignee = fields_data.get("assignee") or {}
            status = fields_data.get("status") or {}
            issue_type = fields_data.get("issuetype") or {}
            tickets.append(
                Ticket(
                    key=issue["key"],
                    summary=fields_data.get("summary") or "",
                    assignee=assignee.get("displayName"),
                    story_points=story_points_value(fields_data.get(story_points_field)),
                    status=(status.get("name") or ""),
                    issue_type=(issue_type.get("name") or ""),
                )
            )
        total = int(data.get("total", len(tickets)))
        start_at += len(issues)
        if start_at >= total or not issues:
            break
    return tickets


def google_credentials(
    service_account_file: Path | None,
    client_file: Path | None,
    token_file: Path,
) -> Credentials | ServiceAccountCredentials:
    if service_account_file and service_account_file.exists():
        creds = ServiceAccountCredentials.from_service_account_file(
            str(service_account_file), scopes=GOOGLE_SCOPES
        )
        creds.refresh(Request())
        print(f"  Google auth: service account ({creds.service_account_email})")
        return creds

    user_creds: Credentials | None = None
    if token_file.exists():
        user_creds = Credentials.from_authorized_user_file(str(token_file), GOOGLE_SCOPES)
    if user_creds and user_creds.expired and user_creds.refresh_token:
        user_creds.refresh(Request())
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(user_creds.to_json(), encoding="utf-8")
    if user_creds and user_creds.valid:
        print("  Google auth: saved OAuth user token")
        return user_creds
    if not client_file or not client_file.exists():
        raise SystemExit(
            "Google credentials not found. Set GOOGLE_SERVICE_ACCOUNT_FILE to a "
            "service-account JSON, or GOOGLE_OAUTH_CLIENT_FILE for browser login."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(client_file), GOOGLE_SCOPES)
    user_creds = flow.run_local_server(port=0, prompt="consent")
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(user_creds.to_json(), encoding="utf-8")
    print("  Google auth: OAuth user login")
    return user_creds


def fetch_sheet_capacity_live(
    spreadsheet_id: str,
    tab_name: str,
    gid: int | None,
    name_col: str,
    hours_col: str,
    service_account_file: Path | None,
    client_file: Path | None,
    token_file: Path,
) -> dict[str, tuple[str, float]]:
    import io

    creds = google_credentials(service_account_file, client_file, token_file)
    if not creds.token:
        raise SystemExit("Google access token is missing after authorization.")
    params: dict[str, str] = {"tqx": "out:csv"}
    if gid is not None:
        params["gid"] = str(gid)
        source = f"gid={gid}"
    else:
        params["sheet"] = tab_name
        source = f"tab '{tab_name}'"
    response = requests.get(
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/gviz/tq",
        params=params,
        headers={"Authorization": f"Bearer {creds.token}"},
        timeout=60,
    )
    if response.status_code >= 400:
        hint = ""
        if service_account_file:
            hint = (
                " Share the sheet with the service account email if it is not already shared."
            )
        raise SystemExit(
            f"Google Sheet download failed ({response.status_code}): {response.text[:400]}.{hint}"
        )
    rows = list(csv.reader(io.StringIO(response.text)))
    print(f"  Sheet: {spreadsheet_id} ({source})")
    return parse_capacity_rows(rows, name_col, hours_col)


def fetch_sheet_capacity_csv(
    csv_path: Path, name_col: str, hours_col: str
) -> dict[str, tuple[str, float]]:
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))
    return parse_capacity_rows(rows, name_col, hours_col)


def parse_capacity_rows(
    rows: list[list[str]], name_col: str, hours_col: str
) -> dict[str, tuple[str, float]]:
    name_letter, name_idx = resolve_column_spec(name_col, rows)
    hours_letter, hours_idx = resolve_column_spec(hours_col, rows)
    print(f"  Username column: {name_letter} (input: {name_col})")
    print(f"  Capacity column: {hours_letter} (input: {hours_col})")
    capacity: dict[str, tuple[str, float]] = {}
    for row in rows:
        if len(row) <= max(name_idx, hours_idx):
            continue
        raw_name = (row[name_idx] or "").strip()
        if not raw_name:
            continue
        key = normalize_name(raw_name)
        if key in HEADER_NAMES or key.startswith("capacity of the team"):
            continue
        hours = parse_hours(row[hours_idx])
        if hours is None:
            continue
        # Keep the first real hours value; later duplicate rows can be 0.
        existing = capacity.get(key)
        if existing and existing[1] != 0:
            continue
        if existing and hours == 0:
            continue
        capacity[key] = (raw_name, hours)
    if not capacity:
        raise SystemExit(
            f"No member hours found in column {name_letter} / {hours_letter}. "
            "Check the username and capacity column inputs."
        )
    return capacity


def match_capacity(
    member_name: str, capacity: dict[str, tuple[str, float]]
) -> tuple[str, float] | None:
    key = normalize_name(member_name)
    if key in capacity:
        return capacity[key]
    return None


def build_report(
    tickets: list[Ticket], capacity: dict[str, tuple[str, float]]
) -> tuple[list[MemberStats], list[Ticket], dict[str, MemberStats]]:
    by_member = {name: MemberStats(name=name) for name in TEAM_MEMBERS}
    lookup = {normalize_name(name): name for name in TEAM_MEMBERS}
    unassigned: list[Ticket] = []
    others: dict[str, MemberStats] = {}

    for ticket in tickets:
        if not ticket.assignee:
            unassigned.append(ticket)
            continue
        team_name = lookup.get(normalize_name(ticket.assignee))
        if team_name:
            stats = by_member[team_name]
        else:
            stats = others.setdefault(ticket.assignee, MemberStats(name=ticket.assignee))
        stats.story_points += ticket.story_points
        stats.ticket_count += 1
        if ticket.story_points:
            stats.tickets_with_points += 1
        stats.tickets.append(ticket)

    for stats in by_member.values():
        matched = match_capacity(stats.name, capacity)
        if matched:
            stats.sheet_name, stats.capacity_hours = matched

    return list(by_member.values()), unassigned, others


def write_member_csv(path: Path, members: list[MemberStats]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "member",
                "sheet_name",
                "capacity_hours",
                "story_points",
                "remaining_hours",
                "occupied_pct",
                "ticket_count",
                "tickets_with_story_points",
            ]
        )
        for stats in members:
            writer.writerow(
                [
                    stats.name,
                    stats.sheet_name or "",
                    "" if stats.capacity_hours is None else stats.capacity_hours,
                    round(stats.story_points, 2),
                    "" if stats.remaining_hours is None else stats.remaining_hours,
                    "" if stats.occupied_pct is None else stats.occupied_pct,
                    stats.ticket_count,
                    stats.tickets_with_points,
                ]
            )


def write_ticket_csv(path: Path, members: list[MemberStats]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["member", "issue_key", "issue_type", "status", "story_points", "summary"]
        )
        for stats in members:
            for ticket in stats.tickets:
                writer.writerow(
                    [
                        stats.name,
                        ticket.key,
                        ticket.issue_type,
                        ticket.status,
                        ticket.story_points,
                        ticket.summary,
                    ]
                )


def print_table(members: list[MemberStats], fix_version: str) -> None:
    print(f"\nPI occupancy for fix version {fix_version}")
    print("-" * 108)
    header = (
        f"{'Member':<28} {'Capacity':>10} {'Story pts':>10} "
        f"{'Remaining':>10} {'Occupied %':>12} {'Tickets':>8}"
    )
    print(header)
    print("-" * 108)
    for stats in members:
        capacity = "-" if stats.capacity_hours is None else f"{stats.capacity_hours:.1f}"
        remaining = "-" if stats.remaining_hours is None else f"{stats.remaining_hours:.1f}"
        occupied = "-" if stats.occupied_pct is None else f"{stats.occupied_pct:.1f}%"
        print(
            f"{stats.name:<28} {capacity:>10} {stats.story_points:>10.1f} "
            f"{remaining:>10} {occupied:>12} {stats.ticket_count:>8}"
        )
    print("-" * 108)
    total_capacity = sum(m.capacity_hours or 0 for m in members)
    total_points = sum(m.story_points for m in members)
    total_remaining = total_capacity - total_points
    occupied = (total_points / total_capacity * 100) if total_capacity else 0
    print(
        f"{'TEAM TOTAL':<28} {total_capacity:>10.1f} {total_points:>10.1f} "
        f"{total_remaining:>10.1f} {occupied:>11.1f}% {sum(m.ticket_count for m in members):>8}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare Jira story points with Google Sheet capacity hours."
    )
    parser.add_argument(
        "--sheet-url",
        help="Google Sheet URL or spreadsheet ID. Prompted if omitted.",
    )
    parser.add_argument(
        "--fix-version",
        help="Jira fix version. Prompted if omitted.",
    )
    parser.add_argument(
        "--name-column",
        help="Google Sheet username column letter or header (e.g. D or Name). Prompted if omitted.",
    )
    parser.add_argument(
        "--capacity-column",
        help="Google Sheet capacity column letter or header (e.g. R). Prompted if omitted.",
    )
    parser.add_argument("--board-name", help="Jira board name (default from .env)")
    parser.add_argument(
        "--capacity-csv",
        help="Use an exported Capacity tab CSV instead of the live Google Sheet",
    )
    parser.add_argument(
        "--output-dir",
        default="output",
        help="Directory for CSV reports (default: output)",
    )
    return parser.parse_args()


def main() -> int:
    project_root = Path(__file__).resolve().parent
    os.chdir(project_root)
    load_env(project_root)
    args = parse_args()
    capacity_csv_raw = args.capacity_csv or os.getenv("CAPACITY_CSV")

    sheet_url = ""
    spreadsheet_id = ""
    sheet_gid: int | None = None
    if not capacity_csv_raw:
        sheet_url = resolve_run_input(
            args.sheet_url,
            "Google Sheet link",
            default_sheet_url(),
        )
        spreadsheet_id, sheet_gid = parse_sheet_link(sheet_url)

    fix_version = resolve_run_input(
        args.fix_version,
        "Fix version",
        os.getenv("FIX_VERSION", "2026.05.V1.0").strip() or "2026.05.V1.0",
    )
    name_col = resolve_run_input(
        args.name_column,
        "Google Sheet username column (letter or header)",
        os.getenv("NAME_COLUMN", "D").strip() or "D",
    )
    hours_col = resolve_run_input(
        args.capacity_column,
        "Google Sheet capacity column (letter or header)",
        os.getenv("CAPACITY_COLUMN", "R").strip() or "R",
    )
    board_name = args.board_name or env_str("BOARD_NAME", "GSIS - Scrum Board")
    sheet_tab = env_str("SHEET_TAB", "capacity")
    story_points_field = os.getenv("STORY_POINTS_FIELD", "customfield_10102").strip()
    pat = env_str("JIRA_PAT")
    base_url = env_str("JIRA_BASE_URL", "https://jira.trimble.tools")

    print("Reading Google Sheet capacity...")
    if capacity_csv_raw:
        capacity_csv = Path(capacity_csv_raw)
        if not capacity_csv.exists():
            raise SystemExit(f"Capacity CSV not found: {capacity_csv}")
        capacity = fetch_sheet_capacity_csv(capacity_csv, name_col, hours_col)
        print(f"  Loaded {len(capacity)} rows from {capacity_csv}")
    else:
        sa_raw = os.getenv(
            "GOOGLE_SERVICE_ACCOUNT_FILE",
            "credentials/service-account.json",
        ).strip()
        service_account_file = Path(sa_raw) if sa_raw else None
        oauth_raw = os.getenv("GOOGLE_OAUTH_CLIENT_FILE", "").strip()
        client_file = Path(oauth_raw) if oauth_raw else None
        token_file = Path(os.getenv("GOOGLE_TOKEN_FILE", "credentials/google_token.json"))
        capacity = fetch_sheet_capacity_live(
            spreadsheet_id,
            sheet_tab,
            sheet_gid,
            name_col,
            hours_col,
            service_account_file,
            client_file,
            token_file,
        )
        print(f"  Loaded {len(capacity)} member rows")

    print("Reading Jira...")
    verify_ssl = env_bool("JIRA_SSL_VERIFY", False)
    session = build_session(verify_ssl)
    board_id = find_board_id(session, base_url, pat, board_name)
    story_points_field = resolve_story_points_field(
        session, base_url, pat, story_points_field
    )
    tickets = fetch_jira_tickets(
        session, base_url, pat, board_id, fix_version, story_points_field
    )
    print(f"  Board '{board_name}' (id {board_id}): {len(tickets)} tickets in {fix_version}")

    members, unassigned, others = build_report(tickets, capacity)
    unmatched = [m.name for m in members if m.capacity_hours is None]
    if unmatched:
        print("  No sheet hours for: " + ", ".join(unmatched))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    output_dir = Path(args.output_dir)
    member_csv = output_dir / f"pi_capacity_{fix_version.replace('.', '-')}_{stamp}.csv"
    ticket_csv = output_dir / f"pi_tickets_{fix_version.replace('.', '-')}_{stamp}.csv"
    write_member_csv(member_csv, members)
    write_ticket_csv(ticket_csv, members)

    extras = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sheet_url": sheet_url,
        "spreadsheet_id": spreadsheet_id,
        "sheet_gid": sheet_gid,
        "fix_version": fix_version,
        "name_column": name_col,
        "capacity_column": hours_col,
        "board_name": board_name,
        "jira_ticket_count": len(tickets),
        "unassigned_tickets": [t.key for t in unassigned],
        "other_assignees": {
            name: {"tickets": stats.ticket_count, "story_points": stats.story_points}
            for name, stats in sorted(others.items())
        },
    }
    extras_path = output_dir / f"pi_extras_{fix_version.replace('.', '-')}_{stamp}.json"
    extras_path.write_text(json.dumps(extras, indent=2), encoding="utf-8")

    print_table(members, fix_version)
    print(f"\nWrote {member_csv}")
    print(f"Wrote {ticket_csv}")
    print(f"Wrote {extras_path}")
    if unassigned:
        print(f"Unassigned tickets: {len(unassigned)}")
    if others:
        print(f"Assignees outside the team list: {len(others)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
