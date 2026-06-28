#!/usr/bin/env python3
"""ラッコM&A 新着YouTubeチャンネル案件 自動監視デーモン。

Tier 1（即時通知）: YouTubeカテゴリ + 月次利益 ≥ TIER1_MIN_PROFIT
Tier 2（研究候補）: YouTubeカテゴリ + 再現性キーワード該当 → DB蓄積のみ

ログ: /root/rakkoma/rakkoma.log
データ: /root/rakkoma/data/seen_listings.json, data/listings/{id}.json
"""

import json
import logging
import os
import sys
import time as _time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ── パス設定 ──────────────────────────────────────────────────────────────────
RAKKOMA_DIR = Path(__file__).parent
sys.path.insert(0, str(RAKKOMA_DIR))
from config import (
    DATA_DIR, LISTINGS_DIR, POLL_INTERVAL_SEC,
    FEED_URL, BASE_URL, UA,
    TIER1_MIN_PROFIT,
    REPLICABLE_CONTENT_KEYWORDS,
    YOUTUBE_TITLE_KEYWORDS,
    ADSENSE_KEYWORDS,
    SOLD_MARKER, WITHDRAWN_MARKER, DEAL_DAYS_RE,
)

LOG_FILE  = RAKKOMA_DIR / "rakkoma.log"
SEEN_FILE = DATA_DIR / "seen_listings.json"

JST = timezone(timedelta(hours=9))

# ── ロギング ──────────────────────────────────────────────────────────────────

class _JSTFormatter(logging.Formatter):
    def converter(self, ts):
        return _time.gmtime(ts + 9 * 3600)

log = logging.getLogger(__name__)

def _setup_logging() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LISTINGS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fmt = _JSTFormatter("%(asctime)s JST [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    h_file   = logging.FileHandler(LOG_FILE, encoding="utf-8")
    h_stdout = logging.StreamHandler(sys.stdout)
    for h in (h_file, h_stdout):
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[h_file, h_stdout])

# ── seen管理 ──────────────────────────────────────────────────────────────────

def _load_seen() -> set[str]:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    return set()

def _save_seen(seen: set[str]) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")

# ── RSS フェッチ ──────────────────────────────────────────────────────────────

def _fetch_feed() -> list[dict]:
    try:
        resp = requests.get(
            FEED_URL,
            headers={"User-Agent": UA, "Accept": "application/xml, text/xml, */*"},
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error(f"フィード取得失敗: {e}")
        return []

    ns = {"a": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        log.error(f"フィードパース失敗: {e}")
        return []

    entries = []
    for entry in root.findall("a:entry", ns):
        eid   = entry.findtext("a:id", namespaces=ns, default="")
        title = entry.findtext("a:title", namespaces=ns, default="")
        pub   = entry.findtext("a:published", namespaces=ns, default="")
        content = entry.findtext("a:content", namespaces=ns, default="")
        link_el = entry.find("a:link[@rel='alternate']", ns)
        url = link_el.get("href", "") if link_el is not None else eid

        pid = eid.rstrip("/").split("/")[-1]
        entries.append({
            "id":      pid,
            "url":     url or f"{BASE_URL}/project/detail/{pid}",
            "title":   title,
            "published": pub,
            "content": content.strip(),
        })
    return entries

# ── タイトル一次フィルター ─────────────────────────────────────────────────────

def _is_video_related_title(title: str) -> bool:
    return any(kw in title for kw in YOUTUBE_TITLE_KEYWORDS)

# ── 詳細ページ取得・パース ─────────────────────────────────────────────────────

import re as _re
import html as _htmllib

def _fetch_detail(pid: str, url: str) -> dict | None:
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning(f"[{pid}] 詳細ページ取得失敗: {e}")
        return None

    html = resp.text

    def _cells() -> list[str]:
        raw = _re.findall(r'<(?:th|dt|td|dd)[^>]*>\s*(.*?)\s*</(?:th|dt|td|dd)>', html, _re.S)
        return [_re.sub(r'<[^>]+>', '', s).strip() for s in raw]

    cells = _cells()

    def _get_field(label: str) -> str:
        for i, c in enumerate(cells):
            if label in c and i + 1 < len(cells):
                raw = cells[i + 1].replace("\n", " ").replace("&#0165;", "¥")
                return _re.sub(r' {2,}', ' ', raw).strip()
        return ""

    def _parse_num(s: str) -> int | None:
        m = _re.search(r'[\d,]+', s.replace("&#0165;", "").replace("¥", ""))
        return int(m.group().replace(",", "")) if m else None

    category      = _get_field("カテゴリ")
    price_str     = _get_field("希望売却価格")
    ratio_str     = _get_field("評価倍率")
    revenue_str   = _get_field("売上/月（直近）")
    profit_str    = _get_field("利益/月（直近）")
    start_date    = _get_field("運営開始時期")
    status        = _get_field("現在の運営状況")
    post_count    = _get_field("投稿数")
    followers_str = _get_field("フォロワー数・登録者数")
    biz_model     = _get_field("収益モデル")
    content_type  = _get_field("コンテンツの性質")

    # og:description を説明文として取得
    desc_m = _re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{0,500})', html)
    description = desc_m.group(1).strip() if desc_m else ""

    # 案件名を og:title / <title> から取得（サイト名サフィックス・HTMLエンティティ除去）
    title_m = (_re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', html)
               or _re.search(r'<title[^>]*>([^<]+)</title>', html))
    page_title = _htmllib.unescape(title_m.group(1)).strip() if title_m else ""
    page_title = _re.sub(r'\s*[|｜]\s*サイト売買のラッコM&A\s*$', '', page_title)

    # ステータス判別（成約済み / 受付終了 / 募集中）と成約期間
    if SOLD_MARKER in html:
        status_state, dm = "成約済み", _re.search(DEAL_DAYS_RE, html)
        deal_days = int(dm.group(1)) if dm else None
    elif WITHDRAWN_MARKER in html:
        status_state, deal_days = "受付終了", None
    else:
        status_state, deal_days = "募集中", None

    return {
        "id":           pid,
        "url":          url,
        "title":        page_title,
        "status_state": status_state,
        "deal_days":    deal_days,
        "category":     category,
        "price":        _parse_num(price_str),
        "price_str":    price_str,
        "ratio_str":    ratio_str,
        "revenue":      _parse_num(revenue_str),
        "revenue_str":  revenue_str,
        "profit":       _parse_num(profit_str),
        "profit_str":   profit_str,
        "start_date":   start_date,
        "status":       status,
        "post_count":   post_count,
        "followers":    _parse_num(followers_str),
        "followers_str": followers_str,
        "biz_model":    biz_model,
        "content_type": content_type,
        "description":  description,
        "fetched_at":   datetime.now(JST).isoformat(),
    }

# ── 分類ロジック ──────────────────────────────────────────────────────────────

def _is_youtube(detail: dict, title: str = "") -> bool:
    # "プラットフォームの提供する収益化プログラム" = YouTube Partner Program
    biz = detail.get("biz_model", "")
    if "プラットフォームの提供する収益化プログラム" in biz:
        return True
    # "登録者" in followers_str は YouTube特有の登録者数表記
    if "登録者" in detail.get("followers_str", ""):
        return True
    # タイトルにYouTube系キーワードがあれば（Stage1を通過済みだが安全網として）
    if title and any(kw in title for kw in YOUTUBE_TITLE_KEYWORDS):
        return True
    return False

def _is_tier1(detail: dict) -> bool:
    profit = detail.get("profit") or 0
    return profit >= TIER1_MIN_PROFIT

def _is_tier2(detail: dict, title: str) -> bool:
    text = " ".join([title, detail.get("description", ""), detail.get("content_type", ""), detail.get("biz_model", "")])
    return any(kw in text for kw in REPLICABLE_CONTENT_KEYWORDS)

def _has_adsense(detail: dict) -> bool:
    return any(kw in detail.get("biz_model", "") for kw in ADSENSE_KEYWORDS)

# ── Slack 通知 ────────────────────────────────────────────────────────────────

def _notify_slack(detail: dict, title: str, tier: int) -> None:
    webhook = os.environ.get("SLACK_WEBHOOK_URL_RAKKOMA", "")
    if not webhook:
        log.warning("SLACK_WEBHOOK_URL_RAKKOMA 未設定 → 通知スキップ")
        return

    tier_label = ":rotating_light: *Tier1 即時通知*" if tier == 1 else ":mag: *Tier2 研究候補*"
    adsense = ":white_check_mark: アドセンスあり" if _has_adsense(detail) else ":x: アドセンスなし"

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"ラッコM&A 新着 [{tier_label.replace('*','').strip()}]"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"{tier_label}\n*{title}*"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*利益/月（直近）*\n{detail.get('profit_str') or '不明'}"},
            {"type": "mrkdwn", "text": f"*希望売却価格*\n{detail.get('price_str') or '不明'}"},
            {"type": "mrkdwn", "text": f"*評価倍率*\n{detail.get('ratio_str') or '不明'}"},
            {"type": "mrkdwn", "text": f"*登録者数*\n{detail.get('followers_str') or '不明'}"},
            {"type": "mrkdwn", "text": f"*カテゴリ*\n{detail.get('category') or '不明'}"},
            {"type": "mrkdwn", "text": f"*収益モデル*\n{adsense}"},
        ]},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*コンテンツ性質*\n{detail.get('content_type') or '不明'}"},
            {"type": "mrkdwn", "text": f"*運営開始*\n{detail.get('start_date') or '不明'}"},
        ]},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "案件を見る"}, "url": detail["url"], "style": "primary"},
        ]},
    ]

    try:
        requests.post(webhook, json={"blocks": blocks}, timeout=10).raise_for_status()
        log.info(f"[{detail['id']}] Slack通知送信 (Tier{tier})")
    except Exception as e:
        log.error(f"[{detail['id']}] Slack通知失敗: {e}")

# ── 案件保存 ──────────────────────────────────────────────────────────────────

def _save_listing(detail: dict, title: str, tier: int) -> None:
    pid = detail["id"]
    out = {**detail, "title": title, "tier": tier}
    path = LISTINGS_DIR / f"{pid}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

# ── メインチェック ────────────────────────────────────────────────────────────

def check_once(dry_run: bool = False) -> int:
    entries = _fetch_feed()
    if not entries:
        return 0

    seen = _load_seen()
    notified = 0

    for entry in entries:
        pid   = entry["id"]
        title = entry["title"]

        if pid in seen:
            continue

        seen.add(pid)
        _save_seen(seen)

        # タイトル一次フィルター（詳細ページ取得の判断）
        if not _is_video_related_title(title):
            log.debug(f"[{pid}] スキップ（動画系タイトルなし）: {title[:50]}")
            continue

        log.info(f"[{pid}] 新着・動画系 → 詳細取得: {title[:60]}")
        detail = _fetch_detail(pid, entry["url"])
        if detail is None:
            continue

        if not _is_youtube(detail, title):
            log.info(f"[{pid}] YouTube非該当 (biz={detail.get('biz_model','')[:40]}): {title[:50]}")
            continue

        # Tier 判定
        if _is_tier1(detail):
            tier = 1
        elif _is_tier2(detail, title):
            tier = 2
        else:
            log.info(f"[{pid}] Tier対象外 (利益={detail.get('profit_str')}, 再現性KWなし): {title[:50]}")
            _save_listing(detail, title, tier=0)
            continue

        log.info(f"[{pid}] Tier{tier} 案件: 利益={detail.get('profit_str')} / 価格={detail.get('price_str')} / {title[:50]}")
        _save_listing(detail, title, tier)

        if dry_run:
            log.info(f"[{pid}] DRY-RUN: Slack通知スキップ")
        else:
            _notify_slack(detail, title, tier)
        notified += 1

    return notified

# ── デーモンループ ─────────────────────────────────────────────────────────────

import argparse

def main() -> None:
    parser = argparse.ArgumentParser(description="ラッコM&A YouTubeチャンネル監視デーモン")
    parser.add_argument("--once",    action="store_true", help="1回チェックして終了")
    parser.add_argument("--dry-run", action="store_true", help="Slack通知しない")
    args = parser.parse_args()

    _setup_logging()
    log.info(f"=== scrape_rakkoma 起動 ===  Tier1閾値={TIER1_MIN_PROFIT:,}円  ポーリング={POLL_INTERVAL_SEC}秒")

    if args.once:
        n = check_once(dry_run=args.dry_run)
        log.info(f"チェック完了: 通知={n}件")
        return

    while True:
        t_start = _time.time()
        try:
            n = check_once(dry_run=args.dry_run)
            log.info(f"ポーリング完了: 通知={n}件")
        except Exception as e:
            log.error(f"予期しないエラー: {e}", exc_info=True)
        elapsed = _time.time() - t_start
        wait = max(0.0, POLL_INTERVAL_SEC - elapsed)
        log.info(f"{int(wait)}秒待機...")
        _time.sleep(wait)

if __name__ == "__main__":
    main()
