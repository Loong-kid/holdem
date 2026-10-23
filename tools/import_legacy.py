"""Idempotently import a private Supabase JSON backup into D1, then verify hashes.

Prepare only: py tools/import_legacy.py <backup.json>
Import:       py tools/import_legacy.py <backup.json> --remote
Local test:   py tools/import_legacy.py <backup.json> --local --persist-to <directory>
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cloudflare.repository import event_payload, event_hash


def normalized_rows(payload):
    rows = payload["hands"] if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("Backup must contain a hand array")
    seen = set()
    for hand in rows:
        if type(hand.get("id")) is not int or hand["id"] < 1 or hand["id"] in seen:
            raise ValueError("Missing or duplicate original hand id")
        seen.add(hand["id"])
        if not isinstance(hand.get("room"), str) or not isinstance(hand.get("events"), list):
            raise ValueError("Missing room or complete events")
        if type(hand.get("hand_number")) is not int or not isinstance(hand.get("played_at"), str):
            raise ValueError("Missing original hand number or timestamp")
        yield {**hand, "source_key": f"supabase:{hand['id']}",
               "session_id": f"supabase:{hand['id']}"}


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def import_sql(rows):
    statements = []
    for hand in rows:
        values = [hand["source_key"], hand["session_id"], hand["room"], hand["hand_number"],
                  hand["played_at"], hand["title"], event_payload(hand["events"]), event_hash(hand["events"])]
        statements.append("INSERT INTO hands(source_key,session_id,room,hand_number,played_at,title,events,event_hash) "
                          "VALUES(" + ",".join(quote(v) for v in values) + ") "
                          "ON CONFLICT(source_key) DO NOTHING;")
    return "\n".join(statements) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path)
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--remote", action="store_true")
    target.add_argument("--local", action="store_true")
    parser.add_argument("--persist-to", default=".wrangler/state")
    args = parser.parse_args()
    raw = args.backup.read_bytes()
    rows = list(normalized_rows(json.loads(raw)))
    out = ROOT / "backups" / ("supabase-import-" + hashlib.sha256(raw).hexdigest()[:12] + ".sql")
    out.parent.mkdir(exist_ok=True)
    out.write_text(import_sql(rows), encoding="utf-8")
    print(f"Prepared {len(rows)} hands. Private SQL: {out}", flush=True)
    if not args.local and not args.remote:
        return
    command = [shutil.which("npm.cmd") or shutil.which("npm"), "exec", "wrangler", "--", "d1", "execute", "DB"]
    location = ["--remote"] if args.remote else ["--local", "--persist-to", args.persist_to]
    subprocess.run(command + ["--file", str(out), *location], cwd=ROOT, check=True)
    query = "SELECT source_key,event_hash FROM hands WHERE source_key LIKE 'supabase:%'"
    check = subprocess.run(command + ["--command", query, "--json", *location], cwd=ROOT,
                           check=True, capture_output=True, encoding="utf-8")
    result = json.loads(check.stdout)
    actual = {r["source_key"]: r["event_hash"] for block in result for r in block.get("results", [])}
    for hand in rows:
        if actual.get(hand["source_key"]) != event_hash(hand["events"]):
            raise RuntimeError("Import verification failed: " + hand["source_key"])
    print(f"Verified all {len(rows)} original identities and event SHA256 hashes", flush=True)


if __name__ == "__main__":
    main()
