#!/usr/bin/env python3
"""バックフィル対象の案件ID確定リストを生成する（詳細ページは取得しない=軽量）。

  1. 販売中(active)一覧を `?page=1..N` で巡回して募集中IDを確定
  2. サイトマップから直近 RECENT_MONTHS ヶ月の lastmod を持つ案件を抽出
  3. 「直近クローズ案件 = 直近 − 募集中」を算出
  4. 募集中 + 直近クローズ を確定リストとして data/enum_targets.json に出力

ステータス（成約済み/受付終了）の確定は詳細取得時に行う。ここでは active由来かを
status_hint で区別するのみ。

  python3 enumerate_listings.py              # 既定: 直近6ヶ月（config.RECENT_MONTHS）
  python3 enumerate_listings.py --months 12
"""

import argparse
import json
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path

import re
import requests

sys.path.insert(0, str(Path(__file__).parent))
from config import (
    BASE_URL, LIST_URL, SITEMAP_LISTINGS, UA,
    RECENT_MONTHS, CRAWL_DELAY_SEC, ENUM_TARGETS_FILE,
)

JST = timezone(timedelta(hours=9))
SM_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def fetch_active_ids() -> set[str]:
    """販売中一覧をページ巡回して募集中の案件IDを集める。"""
    ids: set[str] = set()
    page = 1
    while True:
        r = requests.get(f"{LIST_URL}?page={page}", headers={"User-Agent": UA}, timeout=20)
        r.raise_for_status()
        found = set(re.findall(r"/project/detail/(\d+)", r.text))
        if not found:
            break
        before = len(ids)
        ids |= found
        print(f"  active page{page}: {len(found)}件 (累計 {len(ids)})")
        if len(ids) == before:           # 新規ゼロ＝最終ページ到達
            break
        page += 1
        time.sleep(CRAWL_DELAY_SEC)
    return ids


def fetch_recent_from_sitemap(cutoff: str) -> dict[str, str]:
    """サイトマップから lastmod >= cutoff の {id: lastmod} を返す。"""
    r = requests.get(SITEMAP_LISTINGS, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    recent: dict[str, str] = {}
    for e in root.iter(SM_NS + "url"):
        loc = e.findtext(SM_NS + "loc") or ""
        lm = e.findtext(SM_NS + "lastmod") or ""
        pid = loc.rstrip("/").split("/")[-1]
        if pid.isdigit() and lm >= cutoff:
            recent[pid] = lm
    return recent


def main() -> None:
    ap = argparse.ArgumentParser(description="ラッコM&A 案件ID列挙")
    ap.add_argument("--months", type=int, default=RECENT_MONTHS, help="クローズ案件の鮮度（直近Nヶ月）")
    args = ap.parse_args()

    today = datetime.now(JST).date()
    cutoff = (today - timedelta(days=round(args.months * 30.44))).isoformat()
    print(f"=== 列挙開始 ===  基準日={today}  直近{args.months}ヶ月 (lastmod >= {cutoff})\n")

    print("[1/2] 販売中(active)一覧を巡回...")
    active = fetch_active_ids()
    print(f"  → 募集中: {len(active)} 件\n")

    print("[2/2] サイトマップから直近クローズを抽出...")
    recent = fetch_recent_from_sitemap(cutoff)
    closed = {p: lm for p, lm in recent.items() if p not in active}
    print(f"  → 直近{args.months}ヶ月のlastmod: {len(recent)} 件 / うちクローズ: {len(closed)} 件\n")

    # 確定リスト（募集中はlastmod不明なのでサイトマップ値があれば付与）
    targets = []
    for pid in sorted(active, key=int):
        targets.append({"id": pid, "status_hint": "募集中",
                        "lastmod": recent.get(pid, ""), "url": f"{BASE_URL}/project/detail/{pid}"})
    for pid, lm in sorted(closed.items(), key=lambda x: int(x[0])):
        targets.append({"id": pid, "status_hint": "クローズ",
                        "lastmod": lm, "url": f"{BASE_URL}/project/detail/{pid}"})

    out = {
        "generated_at": datetime.now(JST).isoformat(),
        "cutoff": cutoff,
        "months": args.months,
        "counts": {"active": len(active), "closed": len(closed), "total": len(targets)},
        "targets": targets,
    }
    ENUM_TARGETS_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=== 確定 ===")
    print(f"  募集中    : {len(active):>5} 件")
    print(f"  直近クローズ: {len(closed):>5} 件")
    print(f"  合計対象  : {len(targets):>5} 件")
    print(f"  出力      : {ENUM_TARGETS_FILE}")
    # 礼儀コストの目安
    for d in (CRAWL_DELAY_SEC, 10):
        h = len(targets) * d / 3600
        print(f"  詳細取得見積: 遅延{d:g}秒 → {h:.1f}時間")


if __name__ == "__main__":
    main()
