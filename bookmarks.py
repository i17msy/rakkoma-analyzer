"""★ブックマークの永続化。ダッシュの localStorage からエクスポートした bookmarks.json を DB に取り込む。
DB(bookmarksテーブル)に入れた★は dashboard.py が生成時に種として読み、マシン跨ぎ/再生成耐性を持つ。

使い方:
  python3 bookmarks.py import [bookmarks.json]   # json → DB（和集合・既存は維持）
  python3 bookmarks.py list                       # DB のブックマーク一覧
  python3 bookmarks.py export [out.json]          # DB → json（バックアップ/可搬）
  python3 bookmarks.py rm <案件ID> [<案件ID>...]  # DB から削除
"""
import json
import sys
from datetime import datetime, timezone

import storage


def _conn():
    conn = storage.init()
    conn.execute("CREATE TABLE IF NOT EXISTS bookmarks (listing_id TEXT PRIMARY KEY, added_at TEXT)")
    return conn


def cmd_import(path="bookmarks.json"):
    ids = [str(x) for x in json.load(open(path, encoding="utf-8"))]
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat()
    new = sum(conn.execute("INSERT OR IGNORE INTO bookmarks(listing_id, added_at) VALUES(?,?)", (i, now)).rowcount
              for i in ids)
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0]
    print(f"取り込み: {path} の {len(ids)}件中 新規{new}件 → bookmarks（計{total}件）")


def cmd_list():
    conn = _conn()
    rows = conn.execute("""SELECT b.listing_id, l.status_state, substr(l.title,1,50)
        FROM bookmarks b LEFT JOIN listings l ON l.id = b.listing_id ORDER BY b.added_at""").fetchall()
    print(f"ブックマーク {len(rows)}件:")
    for r in rows:
        print(f"  {r[0]} [{r[1] or '?'}] {r[2] or '(DBに無し)'}")


def cmd_export(out="bookmarks_db.json"):
    conn = _conn()
    ids = [r[0] for r in conn.execute("SELECT listing_id FROM bookmarks ORDER BY added_at")]
    json.dump(ids, open(out, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"書き出し: {len(ids)}件 → {out}")


def cmd_rm(ids):
    conn = _conn()
    n = sum(conn.execute("DELETE FROM bookmarks WHERE listing_id=?", (str(i),)).rowcount for i in ids)
    conn.commit()
    print(f"削除: {n}件")


if __name__ == "__main__":
    a = sys.argv[1:]
    cmd = a[0] if a else ""
    if cmd == "import":
        cmd_import(a[1] if len(a) > 1 else "bookmarks.json")
    elif cmd == "list":
        cmd_list()
    elif cmd == "export":
        cmd_export(a[1] if len(a) > 1 else "bookmarks_db.json")
    elif cmd == "rm" and len(a) > 1:
        cmd_rm(a[1:])
    else:
        print(__doc__)
        sys.exit(2)
