"""Download all stored hand replays from the cloud Postgres into a local JSON file.

Run this on your own PC whenever you want a local copy of the cloud data (handy
as a backup, and as insurance against Render's 90-day free-DB expiry).

Setup (once):
    py -m pip install asyncpg

Usage (PowerShell), pass the **External** Database URL from Render
(holdem_replay_DB -> Connections -> External Database URL):
    py backup_db.py "postgresql://USER:PASS@HOST/DBNAME"

  or set it once in the environment and just run the script:
    $env:HOLDEM_DB_URL = "postgresql://USER:PASS@HOST/DBNAME"
    py backup_db.py

The URL contains your password, so don't paste it into chats or commit it.
This script itself holds no secrets and is safe to keep in the repo.
"""

import asyncio
import json
import os
import ssl
import sys
from datetime import datetime
from pathlib import Path


def get_url() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1].strip()
    url = os.environ.get("HOLDEM_DB_URL")
    if url:
        return url.strip()
    print('DB URL이 필요합니다.\n'
          '  사용법: py backup_db.py "<External Database URL>"\n'
          '  (Render -> holdem_replay_DB -> Connections -> External Database URL)')
    sys.exit(1)


async def main():
    try:
        import asyncpg
    except ImportError:
        print("asyncpg가 필요합니다. 먼저 실행: py -m pip install asyncpg")
        sys.exit(1)

    url = get_url()

    # Render's external endpoint requires SSL; skip cert verification for simplicity.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    print("연결 중...")
    con = await asyncpg.connect(url, ssl=ctx)
    try:
        rows = await con.fetch(
            "SELECT id, room, hand_number, played_at, title, events "
            "FROM hands ORDER BY id"
        )
    finally:
        await con.close()

    out = []
    for r in rows:
        events = r["events"]
        if isinstance(events, str):            # jsonb comes back as text
            events = json.loads(events)
        out.append({
            "id": r["id"],
            "room": r["room"],
            "hand_number": r["hand_number"],
            "played_at": r["played_at"].isoformat() if r["played_at"] else None,
            "title": r["title"],
            "events": events,
        })

    Path("backups").mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = Path("backups") / f"holdem_hands_{stamp}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    by_room: dict[str, int] = {}
    for h in out:
        by_room[h["room"]] = by_room.get(h["room"], 0) + 1

    print(f"\n저장 완료: {path}  ({len(out)} hands)")
    for room, c in sorted(by_room.items()):
        print(f"  - 방 '{room}': {c} hands")
    if not out:
        print("  (저장된 핸드가 아직 없습니다. /health 가 db:true 인지, 게임을 좀 쳤는지 확인하세요.)")


if __name__ == "__main__":
    asyncio.run(main())
