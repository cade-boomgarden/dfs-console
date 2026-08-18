"""Import a profile artifact into the app (login + upload, no curl needed).

    python scripts/import_profiles.py --base https://YOUR-APP.onrender.com \
        --user USERNAME --file profiles_2026_wk01.json

Prompts for the password. Requires httpx (same venv as backfill_odds.py).
"""
from __future__ import annotations

import argparse
import getpass
import sys

import httpx


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="app URL, no trailing slash")
    ap.add_argument("--user", required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--password", default=None,
                    help="omit to be prompted (recommended)")
    args = ap.parse_args()

    pw = args.password or getpass.getpass(f"password for {args.user}: ")
    base = args.base.rstrip("/")

    with httpx.Client(timeout=120) as client:
        r = client.post(f"{base}/api/auth/login",
                        json={"username": args.user, "password": pw})
        if r.status_code != 200:
            sys.exit(f"login failed ({r.status_code}): {r.text}")
        print(f"logged in as {r.json().get('username')}")

        with open(args.file, "rb") as f:
            r = client.post(f"{base}/api/profiles/import",
                            files={"file": (args.file, f, "application/json")})
        if r.status_code != 200:
            sys.exit(f"import failed ({r.status_code}): {r.text}")
        print("imported:", r.json())

        r = client.get(f"{base}/api/profiles", params={"position": "RB"})
        if r.status_code == 200:
            rows = r.json().get("profiles", [])
            print(f"\nsanity check -- {len(rows)} RB profiles; first few:")
            for p in rows[:5]:
                print(f"  {p['name']:24s} {p['team']:4s} {p['label']}")


if __name__ == "__main__":
    main()
