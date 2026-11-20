#!/usr/bin/env python3
"""Publish GSOP IN Verification tickets to the Confluence blocked-tickets page."""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import truststore

truststore.inject_into_ssl()

import requests
import urllib3
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

RELATED_STORAGE = """
<h2>Related</h2>
<ul>
<li><p><a href="https://confluence.trimble.tools/pages/viewpage.action?pageId=1261470823">GSOP Ticket Lifecycle Guidelines: Creation &amp; Post-Implementation</a></p></li>
<li><p><a href="https://confluence.trimble.tools/pages/viewpage.action?pageId=1047521403">Processes</a></p></li>
</ul>
"""


@dataclass
class BlockedTicket:
    key: str
    title: str
    waiting_on: str
    since: datetime | None
    days: int | None
    reason: str
    status: str

    @property
    def browse_url(self) -> str:
        return f"https://jira.trimble.tools/browse/{self.key}"


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


def build_session(verify_ssl: bool) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "PUT"),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.verify = verify_ssl
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return session


def api_request(
    session: requests.Session,
    method: str,
    url: str,
    pat: str,
    **kwargs: Any,
) -> Any:
    headers = kwargs.pop("headers", {})
    headers.setdefault("Authorization", f"Bearer {pat}")
    headers.setdefault("Accept", "application/json")
    response = session.request(method, url, headers=headers, timeout=60, **kwargs)
    if response.status_code >= 400:
        hint = ""
        if response.status_code == 401 and "confluence" in url:
            hint = (
                " Add a Confluence Personal Access Token to CONFLUENCE_PAT in .env "
                "(the Jira token cannot update Confluence)."
            )
        raise SystemExit(
            f"{method} {url} failed ({response.status_code}): {response.text[:500]}.{hint}"
        )
    if not response.content:
        return {}
    return response.json()


def parse_jira_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%f%z")


def normalize_status(name: str) -> str:
    return re.sub(r"[\s_-]+", " ", name).strip().casefold()


def jira_wiki_to_plain(text: str) -> str:
    cleaned = text.replace("\r\n", "\n")
    cleaned = re.sub(r"![^\n!]*!", "", cleaned)
    cleaned = re.sub(r"\[~([^\]]+)\]", r"@\1", cleaned)
    cleaned = re.sub(r"\[([^|\]]+)\|[^\]]+\]", r"\1", cleaned)
    cleaned = re.sub(r"\{[^}]+\}", "", cleaned)
    cleaned = re.sub(r"[*_]+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > 400:
        cleaned = cleaned[:397].rstrip() + "..."
    return cleaned


def find_board_id(
    session: requests.Session, base_url: str, pat: str, board_name: str
) -> int:
    start_at = 0
    while True:
        data = api_request(
            session,
            "GET",
            f"{base_url.rstrip('/')}/rest/agile/1.0/board",
            pat,
            params={"name": board_name, "maxResults": 50, "startAt": start_at},
        )
        for board in data.get("values") or []:
            if board.get("name", "").strip().casefold() == board_name.strip().casefold():
                return int(board["id"])
        if data.get("isLast", True):
            break
        start_at += data.get("maxResults", 50)
    raise SystemExit(f'Could not find Jira board named "{board_name}"')


def search_board_issues(
    session: requests.Session,
    base_url: str,
    pat: str,
    board_id: int,
    status_name: str,
) -> list[dict[str, Any]]:
    jql = f'status = "{status_name}" AND issuetype not in subTaskIssueTypes()'
    issues: list[dict[str, Any]] = []
    start_at = 0
    while True:
        data = api_request(
            session,
            "GET",
            f"{base_url.rstrip('/')}/rest/agile/1.0/board/{board_id}/issue",
            pat,
            params={
                "jql": jql,
                "fields": "summary,assignee,status,comment",
                "maxResults": 50,
                "startAt": start_at,
            },
        )
        page = data.get("issues") or []
        issues.extend(page)
        start_at += len(page)
        if start_at >= int(data.get("total", len(issues))) or not page:
            break
    return issues


def last_comment(issue: dict[str, Any]) -> str:
    comments = ((issue.get("fields") or {}).get("comment") or {}).get("comments") or []
    if not comments:
        return ""
    latest = max(comments, key=lambda item: item.get("created") or "")
    return jira_wiki_to_plain(latest.get("body") or "")


def fetch_changelog_histories(
    session: requests.Session, base_url: str, pat: str, issue_key: str
) -> list[dict[str, Any]]:
    histories: list[dict[str, Any]] = []
    start_at = 0
    while True:
        changelog_url = f"{base_url.rstrip('/')}/rest/api/2/issue/{issue_key}/changelog"
        try:
            data = api_request(
                session,
                "GET",
                changelog_url,
                pat,
                params={"maxResults": 100, "startAt": start_at},
            )
        except SystemExit:
            data = api_request(
                session,
                "GET",
                f"{base_url.rstrip('/')}/rest/api/2/issue/{issue_key}",
                pat,
                params={"expand": "changelog", "fields": "none"},
            )
            return (data.get("changelog") or {}).get("histories") or []
        page = data.get("values") or data.get("histories") or []
        histories.extend(page)
        start_at += len(page)
        if data.get("isLast", start_at >= int(data.get("total", len(histories)))) or not page:
            break
    return histories


def since_in_status(
    histories: list[dict[str, Any]], status_name: str
) -> datetime | None:
    target = normalize_status(status_name)
    latest: datetime | None = None
    for history in histories:
        for item in history.get("items") or []:
            if item.get("field") != "status":
                continue
            if normalize_status(item.get("toString") or "") != target:
                continue
            moved = parse_jira_datetime(history.get("created"))
            if moved and (latest is None or moved > latest):
                latest = moved
    return latest


def days_since(moved: datetime | None, now: datetime) -> int | None:
    if moved is None:
        return None
    return max(0, (now.date() - moved.astimezone(timezone.utc).date()).days)


def build_rows(
    session: requests.Session,
    base_url: str,
    pat: str,
    issues: list[dict[str, Any]],
    status_name: str,
    now: datetime,
) -> list[BlockedTicket]:
    rows: list[BlockedTicket] = []
    for issue in issues:
        fields = issue.get("fields") or {}
        assignee = fields.get("assignee") or {}
        status = fields.get("status") or {}
        histories = fetch_changelog_histories(session, base_url, pat, issue["key"])
        moved = since_in_status(histories, status_name)
        rows.append(
            BlockedTicket(
                key=issue["key"],
                title=fields.get("summary") or "",
                waiting_on=assignee.get("displayName") or "",
                since=moved,
                days=days_since(moved, now),
                reason=last_comment(issue),
                status=(status.get("name") or ""),
            )
        )
    rows.sort(key=lambda row: (-(row.days or -1), row.key))
    return rows


def cell(value: str) -> str:
    return f"<td><p>{escape(value) if value else ''}</p></td>"


def build_storage(
    rows: list[BlockedTicket],
    now: datetime,
    status_name: str,
    heading: str = "IN Verification — waiting on stakeholder",
) -> str:
    stamp = now.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    intro = (
        f"<p>Tickets currently in <strong>{escape(status_name)}</strong> on "
        f"<strong>GSIS - Scrum Board</strong>. "
        f"<strong>Waiting on</strong> is the current Jira assignee. "
        f"<strong>Since</strong> / <strong>Days</strong> use the last move into "
        f"{escape(status_name)}. <strong>Reason</strong> is the latest comment.</p>"
        f"<p>Last updated {escape(stamp)}. {len(rows)} ticket(s).</p>"
        f"<h2>{escape(heading)}</h2>"
    )
    header = (
        "<table class=\"wrapped\" data-layout=\"full-width\">"
        "<thead><tr>"
        "<th><p>Ticket</p></th><th><p>Title</p></th><th><p>Waiting on</p></th>"
        "<th><p>Since</p></th><th><p>Days</p></th><th><p>Reason</p></th>"
        "<th><p>Status</p></th>"
        "</tr></thead><tbody>"
    )
    body: list[str] = []
    for row in rows:
        since_text = row.since.date().isoformat() if row.since else ""
        days_text = "" if row.days is None else str(row.days)
        ticket = (
            f'<td><p><a href="{escape(row.browse_url)}">{escape(row.key)}</a></p></td>'
        )
        body.append(
            "<tr>"
            + ticket
            + cell(row.title)
            + cell(row.waiting_on)
            + cell(since_text)
            + cell(days_text)
            + cell(row.reason)
            + cell(row.status)
            + "</tr>"
        )
    if not body:
        body.append(
            "<tr><td colspan=\"7\"><p>No tickets are currently in "
            f"{escape(status_name)}.</p></td></tr>"
        )
    return intro + header + "".join(body) + "</tbody></table>" + RELATED_STORAGE


def write_local_report(path: Path, rows: list[BlockedTicket]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["ticket", "title", "waiting_on", "since", "days", "reason", "status"]
        )
        for row in rows:
            writer.writerow(
                [
                    row.key,
                    row.title,
                    row.waiting_on,
                    row.since.date().isoformat() if row.since else "",
                    "" if row.days is None else row.days,
                    row.reason,
                    row.status,
                ]
            )


def update_confluence(
    session: requests.Session,
    base_url: str,
    pat: str,
    page_id: str,
    title: str,
    storage: str,
    version_message: str = "Nightly IN Verification refresh from Jira",
) -> int:
    current = api_request(
        session,
        "GET",
        f"{base_url.rstrip('/')}/rest/api/content/{page_id}",
        pat,
        params={"expand": "version,body.storage"},
    )
    next_version = int((current.get("version") or {}).get("number", 0)) + 1
    payload = {
        "id": page_id,
        "type": "page",
        "title": title,
        "version": {
            "number": next_version,
            "message": version_message,
        },
        "body": {"storage": {"value": storage, "representation": "storage"}},
    }
    api_request(
        session,
        "PUT",
        f"{base_url.rstrip('/')}/rest/api/content/{page_id}",
        pat,
        headers={"Content-Type": "application/json"},
        json=payload,
    )
    return next_version


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refresh the Confluence blocked-tickets page from IN Verification issues."
    )
    parser.add_argument("--board-name", help="Jira board name")
    parser.add_argument("--status", help='Jira status name (default: "IN Verification")')
    parser.add_argument("--page-id", help="Confluence page ID")
    parser.add_argument("--page-title", help="Confluence page title")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch Jira and write a local CSV, but do not update Confluence",
    )
    parser.add_argument("--output-dir", default="output")
    return parser.parse_args()


def main() -> int:
    project_root = Path(__file__).resolve().parent
    os.chdir(project_root)
    load_env(project_root)
    args = parse_args()

    jira_base = env_str("JIRA_BASE_URL", "https://jira.trimble.tools")
    jira_pat = env_str("JIRA_PAT")
    confluence_base = os.getenv("CONFLUENCE_BASE_URL", "https://confluence.trimble.tools").strip()
    confluence_pat = os.getenv("CONFLUENCE_PAT", "").strip() or jira_pat
    board_name = args.board_name or env_str("BOARD_NAME", "GSIS - Scrum Board")
    status_name = args.status or os.getenv("IN_VERIFICATION_STATUS", "IN Verification").strip()
    page_id = args.page_id or env_str("CONFLUENCE_PAGE_ID", "1261831310")
    page_title = args.page_title or os.getenv(
        "CONFLUENCE_PAGE_TITLE", "Tickets blocked by stakeholder"
    ).strip()
    verify_ssl = env_bool("JIRA_SSL_VERIFY", False)
    now = datetime.now(timezone.utc)

    print(f"Reading Jira board '{board_name}' for status '{status_name}'...")
    session = build_session(verify_ssl)
    board_id = find_board_id(session, jira_base, jira_pat, board_name)
    issues = search_board_issues(session, jira_base, jira_pat, board_id, status_name)
    print(f"  Found {len(issues)} ticket(s)")
    rows = build_rows(session, jira_base, jira_pat, issues, status_name, now)

    stamp = now.strftime("%Y%m%d")
    output_dir = Path(args.output_dir)
    csv_path = output_dir / f"in_verification_{stamp}.csv"
    html_path = output_dir / f"in_verification_{stamp}.html"
    storage = build_storage(rows, now, status_name)
    write_local_report(csv_path, rows)
    html_path.write_text(storage, encoding="utf-8")
    print(f"Wrote {csv_path}")
    print(f"Wrote {html_path}")

    if args.dry_run:
        print("Dry run: Confluence was not updated.")
        return 0

    print(f"Updating Confluence page {page_id}...")
    version = update_confluence(
        session, confluence_base, confluence_pat, page_id, page_title, storage
    )
    print(
        "Updated "
        f"https://confluence.trimble.tools/spaces/GS/pages/{page_id}/"
        f"Tickets+blocked+by+stakeholder (version {version})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
