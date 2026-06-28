#!/usr/bin/env python3
"""enum_targets.json の対象案件を巡回取得して SQLite に格納する（LLM評価はしない）。

  python3 backfill.py --limit 50          # 先頭50件（パイロット）
  python3 backfill.py --state 募集中        # 募集中のみ
  python3 backfill.py                       # 全対象（数時間・礼儀遅延つき）
  python3 backfill.py --refresh            # 取得済みも再取得（既定はスキップ）

robots.txt の Crawl-delay 尊重のため config.CRAWL_DELAY_SEC 間隔でアクセス。
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import ENUM_TARGETS_FILE, CRAWL_DELAY_SEC
import scrape_rakkoma as S
import metrics as M
import storage as DB


def main() -> None:
    ap = argparse.ArgumentParser(description="ラッコM&A バックフィル（詳細取得→SQLite）")
    ap.add_argument("--limit", type=int, help="先頭N件のみ")
    ap.add_argument("--state", choices=["募集中", "クローズ"], help="status_hintで絞る")
    ap.add_argument("--delay", type=float, default=CRAWL_DELAY_SEC, help="リクエスト間隔（秒）")
    ap.add_argument("--refresh", action="store_true", help="取得済みも再取得")
    args = ap.parse_args()

    if not ENUM_TARGETS_FILE.exists():
        sys.exit(f"[ERROR] {ENUM_TARGETS_FILE} が無い。先に enumerate_listings.py を実行してください。")
    data = json.loads(ENUM_TARGETS_FILE.read_text(encoding="utf-8"))
    targets = data["targets"]
    if args.state:
        targets = [t for t in targets if t["status_hint"] == args.state]
    if args.limit:
        targets = targets[:args.limit]

    conn = DB.init()
    done = {r[0] for r in conn.execute("SELECT id FROM listings").fetchall()}

    total = len(targets)
    fetched = skipped = failed = 0
    by_state: dict[str, int] = {}
    print(f"=== バックフィル開始 ===  対象={total}件  遅延={args.delay:g}秒  既取得={len(done)}件\n")

    for i, t in enumerate(targets, 1):
        pid = t["id"]
        if int(pid) in done and not args.refresh:
            skipped += 1
            continue
        detail = S._fetch_detail(pid, t["url"])
        if detail is None:
            failed += 1
            print(f"  [{i}/{total}] {pid} 取得失敗")
            time.sleep(args.delay)
            continue
        met = M.compute(detail)
        DB.upsert_listing(conn, detail, met, title=detail.get("title", ""))
        st = detail.get("status_state", "?")
        by_state[st] = by_state.get(st, 0) + 1
        fetched += 1
        if fetched % 10 == 0 or total <= 60:
            dd = f" / 成約{detail['deal_days']}日" if detail.get("deal_days") else ""
            print(f"  [{i}/{total}] {pid} {st}{dd} 利益={detail.get('profit_str') or '–'} {detail.get('category','')[:18]}")
        time.sleep(args.delay)

    print(f"\n=== 完了 ===  取得={fetched} / スキップ={skipped} / 失敗={failed}")
    print(f"  ステータス内訳: {by_state}")
    print(f"  DB件数: {DB.counts(conn)}")


if __name__ == "__main__":
    main()
