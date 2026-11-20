#!/usr/bin/env python3
"""Publish GSOP Blocker tickets to the Confluence blocked-state page."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from update_in_verification import (
    build_rows,
    build_session,
    build_storage,
    env_bool,
    env_str,
    find_board_id,
    load_env,
    search_board_issues,
    update_confluence,
    write_local_report,
)

PAGE_URL = (
    "https://confluence.trimble.tools/spaces/GS/pages/1303283178/"
    "Tickets+in+Blocked+state"
)
HEADING = "Blocker — waiting on assignee"
VERSION_MESSAGE = "Weekday Blocker refresh from Jira"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refresh the Confluence blocked-state page from Jira Blocker issues."
    )
    parser.add_argument("--board-name", help="Jira board name")
    parser.add_argument("--status", help='Jira status name (default: "Blocker")')
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
    status_name = args.status or os.getenv("BLOCKED_STATUS", "Blocker").strip()
    page_id = args.page_id or os.getenv("BLOCKED_CONFLUENCE_PAGE_ID", "1303283178").strip()
    page_title = args.page_title or os.getenv(
        "BLOCKED_CONFLUENCE_PAGE_TITLE", "Tickets in 'Blocked' state"
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
    csv_path = output_dir / f"blocked_{stamp}.csv"
    html_path = output_dir / f"blocked_{stamp}.html"
    storage = build_storage(rows, now, status_name, heading=HEADING)
    write_local_report(csv_path, rows)
    html_path.write_text(storage, encoding="utf-8")
    print(f"Wrote {csv_path}")
    print(f"Wrote {html_path}")

    if args.dry_run:
        print("Dry run: Confluence was not updated.")
        return 0

    print(f"Updating Confluence page {page_id}...")
    version = update_confluence(
        session,
        confluence_base,
        confluence_pat,
        page_id,
        page_title,
        storage,
        version_message=VERSION_MESSAGE,
    )
    print(f"Updated {PAGE_URL} (version {version})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
