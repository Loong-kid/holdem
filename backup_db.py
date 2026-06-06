"""Download all stored hand replays from the cloud Postgres into a local JSON file.

Run this on your own PC whenever you want a local copy of the cloud data (handy
as a backup, and as insurance against a free DB's expiry).

Setup (once):
    py -m pip install "psycopg[binary]"

Usage (PowerShell), pass your database connection URL
(Supabase: Connect -> Session pooler; or Render: External Database URL):
    py backup_db.py "postgresql://USER:PASS@HOST:5432/DBNAME"

  or set it once in the environment and just run the script:
    $env:HOLDEM_DB_URL = "postgresql://USER:PASS@HOST:5432/DBNAME"
    py backup_db.py

The URL contains your password, so don't paste it into chats or commit it.
This script itself holds no secrets and is safe to keep in the repo.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit


def get_url() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1].strip()
    url = os.environ.get("HOLDEM_DB_URL")
    if url:
        return url.strip()
    print('DB URL이 필요합니다.\n'
          '  사용법: py backup_db.py "<연결 URL>"\n'
          '  (Supabase: Connect -> Session pooler, 또는 Render: External Database URL)')
    sys.exit(1)


def main():
    try:
        import psycopg
    except ImportError:
        print('psycopg가 필요합니다. 먼저 실행: py -m pip install "psycopg[binary]"')
        sys.exit(1)

    url = get_url()
    p = urlsplit(url)
    params = {
        "host": p.hostname,
        "port": p.port or 5432,
        "dbname": (p.path or "/postgres").lstrip("/") or "postgres",
        "sslmode": "require",
    }
    if p.username:
        params["user"] = unquote(p.username)
    if p.password:
        params["password"] = unquote(p.password)
    conninfo = psycopg.conninfo.make_conninfo(**params)

    print("연결 중...")
    with psycopg.connect(conninfo) as con:
        cur = con.execute(
            "SELECT id, room, hand_number, played_at, title, events "
            "FROM hands ORDER BY id"
        )
        rows = cur.fetchall()

    out = []
    for r in rows:
        out.append({
            "id": r[0],
            "room": r[1],
            "hand_number": r[2],
            "played_at": r[3].isoformat() if r[3] else None,
            "title": r[4],
            "events": r[5],          # psycopg returns jsonb as Python objects
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
    main()
