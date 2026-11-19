from __future__ import annotations

import argparse
import json
from pathlib import Path

from bellhaven.crm import CRMError, LiveCRM
from bellhaven.credentials import CredentialError, read_token_file
from bellhaven.models import fingerprint
from bellhaven.pipeline import run_demo, run_pipeline
from bellhaven.scrape import WebsiteError, scrape_website
from bellhaven.store import Store, StoreError


def main() -> None:
    parser = argparse.ArgumentParser(description="Approval-only Bellhaven reconciliation.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    demo = subparsers.add_parser("demo", help="Refresh the persistent synthetic offline workspace.")
    demo.add_argument("--db", default="data/demo.sqlite")
    audit = subparsers.add_parser("audit", help="Print the local decision audit.")
    audit.add_argument("--db", default="data/demo.sqlite")
    sync = subparsers.add_parser("sync", help="Future owner-confirmed read-only live collection.")
    sync.add_argument("--db", default="data/live.sqlite")
    sync.add_argument("--token-file", required=True)
    sync.add_argument("--parent-id", required=True)
    sync.add_argument("--owner-confirmed-live-test", action="store_true")
    arguments = parser.parse_args()
    try:
        if arguments.command == "demo":
            result = run_demo(Store(arguments.db, "demo-v1"))
        elif arguments.command == "audit":
            if not Path(arguments.db).is_file():
                raise StoreError("No local demo database exists yet. Run the demo first.")
            result = Store(arguments.db, "demo-v1").audit()
        else:
            if not arguments.owner_confirmed_live_test:
                raise ValueError("Live collection is locked. Obtain the owner's explicit confirmation first.")
            token = read_token_file(arguments.token_file)
            store = Store(arguments.db, "live:" + fingerprint(token))
            crm = LiveCRM(token, confirmed=True)
            website = scrape_website()
            result = run_pipeline(store, crm, website, arguments.parent_id, allow_live=True)
        print(json.dumps(result, indent=2))
    except (CRMError, CredentialError, StoreError, WebsiteError, ValueError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
