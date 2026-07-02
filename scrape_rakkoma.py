#!/usr/bin/env python3
"""ラッコM&A 新着YouTubeチャンネル案件 自動監視デーモン。

新着を SQLite(data/rakkoma.db) に一元化し、動画系は LLM 評価。
買い/様子見 × 適合≥NOTIFY_MIN_FIT × 総合≥NOTIFY_MIN_OVERALL の厳選のみ Slack 通知。
複数コンテナ共存時は idle_hosts.txt / 環境変数 RAKKOMA_IDLE で重複ポーリングを防ぐ。

ログ: /root/rakkoma/rakkoma.log   データ: /root/rakkoma/data/rakkoma.db
"""

import json
import logging
import os
import socket
import subprocess
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
    YOUTUBE_TITLE_KEYWORDS, ADSENSE_KEYWORDS,
    SOLD_MARKER, WITHDRAWN_MARKER, DEAL_DAYS_RE,
    NOTIFY_MIN_FIT, NOTIFY_MIN_OVERALL, NOTIFY_VERDICTS,
    IDLE_HOSTS_FILE,
    BACKUPS_DIR, HEARTBEAT_STALE_MULT,
)
import storage
import metrics
import dashboard

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

    # 譲渡物種別（ラッコ公式分類・JSON-LD）。YouTube/EC/ブログ/アプリ等を正確に判定するため全種別を保持
    #   例: "アカウント（YouTube）" / "WEBメディア（ブログ/アフィリエイトサイト）" / "ECサイト" …
    at_m = _re.search(r'"name"\s*:\s*"譲渡物種別"\s*,\s*"value"\s*:\s*"([^"]+)"', html)
    asset_type = _htmllib.unescape(at_m.group(1)).strip() if at_m else ""

    # og:description を説明文として取得
    desc_m = _re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{0,500})', html)
    description = desc_m.group(1).strip() if desc_m else ""

    # 案件名を og:title / <title> から取得（サイト名サフィックス・HTMLエンティティ除去）
    title_m = (_re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', html)
               or _re.search(r'<title[^>]*>([^<]+)</title>', html))
    page_title = _htmllib.unescape(title_m.group(1)).strip() if title_m else ""
    page_title = _re.sub(r'\s*[|｜]\s*サイト売買のラッコM&A\s*$', '', page_title)

    # 公開日（ラッコ掲載日）・更新日 → ISO(YYYY-MM-DD)
    def _date_after(label: str) -> str:
        m = _re.search(label + r'.{0,80}?(\d{4})/(\d{1,2})/(\d{1,2})', html, _re.S)
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ""
    listed_at  = _date_after("公開日")
    updated_at = _date_after("更新日")

    # ステータス判別（成約済み / 受付終了 / 募集中）と成約期間
    if SOLD_MARKER in html:
        status_state, dm = "成約済み", _re.search(DEAL_DAYS_RE, html)
        deal_days = int(dm.group(1)) if dm else None
    elif WITHDRAWN_MARKER in html:
        status_state, deal_days = "受付終了", None
    else:
        status_state, deal_days = "募集中", None

    # 月次収益系列（グラフ部品 :data-list の配列。3点要約より信頼できる生データ）
    series_m = _re.search(r'<list-content-area-chart[^>]*:data-list="(\[[\d,\.\s]+\])"', html)
    try:
        profit_series = json.loads(_htmllib.unescape(series_m.group(1))) if series_m else []
    except Exception:
        profit_series = []

    return {
        "id":           pid,
        "url":          url,
        "title":        page_title,
        "profit_series": profit_series,
        "listed_at":    listed_at,
        "updated_at":   updated_at,
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
        "asset_type":   asset_type,
        "description":  description,
        "fetched_at":   datetime.now(JST).isoformat(),
    }

# ── 分類ロジック ──────────────────────────────────────────────────────────────

def _is_youtube(detail: dict, title: str = "") -> bool:
    # 公式分類(譲渡物種別)があれば最優先で確定判定。タイトル推定より信頼でき、
    # 「YouTube種別なのにタイトルに語が無い」取りこぼしも、「非YouTubeなのにタイトルに語がある」誤検出も両方防ぐ。
    at = detail.get("asset_type", "")
    if at:
        return "YouTube" in at
    # asset_type が取れない時のみ従来ヒューリスティック（安全網）
    # "プラットフォームの提供する収益化プログラム" = YouTube Partner Program
    biz = detail.get("biz_model", "")
    if "プラットフォームの提供する収益化プログラム" in biz:
        return True
    # "登録者" in followers_str は YouTube特有の登録者数表記
    if "登録者" in detail.get("followers_str", ""):
        return True
    # タイトルにYouTube系キーワードがあれば（最後の安全網）
    if title and any(kw in title for kw in YOUTUBE_TITLE_KEYWORDS):
        return True
    return False

# ── LLM評価クライアント ────────────────────────────────────────────────────────

def _get_client():
    """ANTHROPIC_API_KEY があれば anthropic クライアント、無ければ None。"""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    import anthropic
    return anthropic.Anthropic(api_key=key)

# ── 厳選通知（評価ベース・「ダッシュボードを見るべき」）──────────────────────────

def _should_notify(ev: dict) -> bool:
    """買い/様子見 × 適合≥N × 総合≥M を満たす案件だけ通知（厳選）。"""
    return ((ev.get("capability_fit") or 0) >= NOTIFY_MIN_FIT
            and ev.get("verdict") in NOTIFY_VERDICTS
            and (ev.get("overall_score") or 0) >= NOTIFY_MIN_OVERALL)

def _notify_dashboard(detail: dict, ev: dict, met: dict, title: str) -> None:
    webhook = os.environ.get("SLACK_WEBHOOK_URL_RAKKOMA", "")
    if not webhook:
        log.warning("SLACK_WEBHOOK_URL_RAKKOMA 未設定 → 通知スキップ")
        return
    vmark = {"買い": ":fire:", "様子見": ":eyes:"}.get(ev.get("verdict"), "")
    flags = " ".join(met.get("flags") or []) or "なし"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "🦦 要確認の新着 — ダッシュボードで精査を"}},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"{vmark} *{ev.get('verdict')}*  ・  総合 *{ev.get('overall_score')}* / 適合 *{ev.get('capability_fit')}*  ・  ID `{detail['id']}`\n*{title}*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"_{ev.get('summary','')}_"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*希望価格*\n{detail.get('price_str') or '不明'}"},
            {"type": "mrkdwn", "text": f"*月利益(直近)*\n{detail.get('profit_str') or '不明'}"},
            {"type": "mrkdwn", "text": f"*登録者*\n{detail.get('followers_str') or '不明'}"},
            {"type": "mrkdwn", "text": f"*フラグ*\n{flags}"},
        ]},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"ダッシュボードで ID `{detail['id']}` を検索して精査してください"}]},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "ラッコで案件を見る"}, "url": detail["url"], "style": "primary"},
        ]},
    ]
    try:
        requests.post(webhook, json={"blocks": blocks}, timeout=10).raise_for_status()
        log.info(f"[{detail['id']}] 厳選通知: {ev.get('verdict')} 総合{ev.get('overall_score')} 適合{ev.get('capability_fit')}")
    except Exception as e:
        log.error(f"[{detail['id']}] 通知失敗: {e}")

# ── メインチェック（SQLite一元化 + 新着LLM評価 + 厳選通知）─────────────────────

def check_once(dry_run: bool = False) -> int:
    entries = _fetch_feed()
    if not entries:
        return 0

    seen = _load_seen()
    conn = storage.init()
    client = _get_client()
    if client is None:
        log.warning("ANTHROPIC_API_KEY 未設定 → 評価・通知なし（DB保存のみ）")

    notified = added = 0
    for entry in entries:
        pid, title = entry["id"], entry["title"]
        if pid in seen:
            continue
        seen.add(pid)
        _save_seen(seen)

        detail = _fetch_detail(pid, entry["url"])
        if detail is None:
            continue

        # 全新着をSQLiteへ（メトリクス・系列・フラグ込み）
        met = metrics.compute(detail)
        storage.upsert_listing(conn, detail, met, title=detail.get("title") or title)
        added += 1

        # 動画系のみLLM評価（物販・Webサービス等はDB保存のみ）。鍵が無ければ評価せず
        if not _is_youtube(detail, title) or client is None:
            log.info(f"[{pid}] DB保存のみ（非動画 or 鍵なし）: {title[:40]}")
            continue
        try:
            import analyze
            _m, ev, _u = analyze.evaluate(client, detail)
        except Exception as e:
            log.error(f"[{pid}] 評価失敗: {e}")
            continue
        storage.save_evaluation(conn, pid, ev)
        log.info(f"[{pid}] 評価: {ev['verdict']} 総合{ev['overall_score']} 適合{ev['capability_fit']} | {title[:32]}")

        if _should_notify(ev):
            if dry_run:
                log.info(f"[{pid}] DRY-RUN: 厳選通知スキップ（{ev['verdict']}）")
            else:
                _notify_dashboard(detail, ev, met, detail.get("title") or title)
            notified += 1

    # 新着があればダッシュボードを再生成（ライブ更新）
    # サブプロセスで実行 → 常駐デーモンの import 済み(古い)モジュールではなく
    # ディスク上の最新 dashboard.py を毎回使う（編集が再起動なしで反映される）
    if added:
        try:
            subprocess.run([sys.executable, str(RAKKOMA_DIR / "dashboard.py")],
                           check=True, cwd=str(RAKKOMA_DIR))
        except Exception as e:
            log.error(f"ダッシュボード再生成失敗: {e}")
    return notified

# ── 死活監視ハートビート＋DBバックアップ（R2/ローカル）──────────────────────

def _pulse(notified=None) -> None:
    """毎ポーリング: R2へ生死信号（heartbeat）を打ち、日次でDBをバックアップする。
    R2未設定でもローカル日次バックアップは動く。失敗しても監視本体は止めない。"""
    try:
        import r2
        conn = storage.init()
        n_l = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        n_e = conn.execute(
            "SELECT COUNT(*) FROM evaluations WHERE overall_score IS NOT NULL").fetchone()[0]
        now = datetime.now(JST)
        stats = {
            "service": "rakkoma-observer",
            "host": socket.gethostname(),
            # MLBの cloud routine と互換: last_poll(epoch)で elapsed 判定できる
            "last_poll": int(now.timestamp()),
            "last_poll_jst": now.strftime("%Y-%m-%d %H:%M:%S"),
            # 自己記述（routineがしきい値をハードコードせず読めるよう同梱）
            "ts": now.isoformat(),
            "interval_sec": POLL_INTERVAL_SEC,
            "stale_after_sec": POLL_INTERVAL_SEC * HEARTBEAT_STALE_MULT,
            "listings": n_l,
            "evaluated": n_e,
            "last_poll_notified": notified,
        }
        hb = r2.put_heartbeat(stats)
        bk = r2.daily_backup(storage.DB_FILE, BACKUPS_DIR, now.strftime("%Y%m%d"))
        log.info(f"pulse: heartbeat={'OK' if hb else 'skip'} "
                 f"backup(local={bk['local']}, r2={bk['r2']})")
    except Exception as e:
        log.warning(f"pulse失敗（監視/バックアップ・本体継続）: {e}")


def _daily_match() -> None:
    """1日1回、YouTube候補照合バッチを回す（＝ローカルルーティン本体・cloud routineの代替）。
    前回の続きから優先順（募集中→成約済み→受付終了）で処理し、quota上限で安全中断。
    idle側では main() のループに来ないので呼ばれない＝稼働中の observer だけが担当。
    ※quota枯渇で0件だった日は「実施済み」にせず、quota回復(≈16時JST)後の次ポーリングで再試行する
      （マーカーは"進捗があった/対象が尽きた"時だけ書く。早朝にquota枯渇で空振り→終日休眠する不具合を回避）。"""
    try:
        import match, storage
        conn = storage.init()
        marker = DATA_DIR / "last_match_date.txt"
        today = datetime.now(JST).strftime("%Y-%m-%d")
        if marker.exists() and marker.read_text().strip() == today:
            return                                          # 今日は実施済み → no-op
        # 未照合ターゲットが尽きていれば実施済みマークして終わり（毎ポーリングでの無駄叩き防止）
        remain = conn.execute("""SELECT COUNT(*) FROM listings l JOIN evaluations e ON e.listing_id=l.id
            WHERE l.status_state IN ('募集中','成約済み','受付終了') AND e.verdict IN ('買い','様子見')
              AND e.capability_fit>=4 AND l.asset_type LIKE '%YouTube%'
              AND l.id NOT IN (SELECT listing_id FROM channel_candidates)""").fetchone()[0]
        if remain == 0:
            marker.write_text(today)
            return
        before = conn.execute("SELECT COUNT(DISTINCT listing_id) FROM channel_candidates").fetchone()[0]
        log.info(f"=== 日次マッチ開始（未照合{remain}件）===")
        match.batch()                                       # 冪等・quota上限で自動中断・末尾でダッシュ再生成
        after = conn.execute("SELECT COUNT(DISTINCT listing_id) FROM channel_candidates").fetchone()[0]
        if after > before:                                  # 進捗があった日だけ実施済みに（quota枯渇の空振りは翌ポーリングで再試行）
            marker.write_text(today)
            log.info(f"=== 日次マッチ完了（+{after - before}件）===")
        else:
            log.info("日次マッチ: 進捗0（quota枯渇の可能性）→ マーカー書かず次ポーリングで再試行")
    except Exception as e:
        log.warning(f"日次マッチ失敗（本体継続）: {e}")


# ── アイドル判定（重複ポーリング防止）─────────────────────────────────────────

def _is_idle_host() -> bool:
    """このコンテナを「監視させない（アイドル）」対象として扱うか判定する。

    複数コンテナ（例: 旧 watcher と新 observer）が同じリポジトリをマウントして
    共存する際、両方がポーリングすると seen 競合・二重通知・二重リクエストになる。
    そこで daemon は起動時に自分の素性を確認し、下記いずれかに該当すれば監視しない:
      1) 環境変数 RAKKOMA_IDLE が真         … 将来コンテナを作成時に -e で指定する用
      2) 自分のホスト名(=コンテナ短縮ID)が IDLE_HOSTS_FILE に記載 … 既存コンテナ向け
    restart でコードが再読込されても自分でアイドル化するので、凍結が永続化する。
    """
    if os.environ.get("RAKKOMA_IDLE", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    try:
        host = socket.gethostname().strip()
        if IDLE_HOSTS_FILE.exists():
            for line in IDLE_HOSTS_FILE.read_text(encoding="utf-8").splitlines():
                e = line.strip()
                if e and not e.startswith("#") and e == host:
                    return True
    except Exception as e:
        log.warning(f"アイドル判定エラー（無視して通常稼働）: {e}")
    return False


# ── デーモンループ ─────────────────────────────────────────────────────────────

import argparse

def main() -> None:
    parser = argparse.ArgumentParser(description="ラッコM&A YouTubeチャンネル監視デーモン")
    parser.add_argument("--once",    action="store_true", help="1回チェックして終了")
    parser.add_argument("--dry-run", action="store_true", help="Slack通知しない")
    args = parser.parse_args()

    _setup_logging()
    log.info(f"=== scrape_rakkoma 起動 ===  ポーリング={POLL_INTERVAL_SEC}秒  "
             f"厳選通知=判定{'/'.join(NOTIFY_VERDICTS)}・適合≥{NOTIFY_MIN_FIT}・総合≥{NOTIFY_MIN_OVERALL}")

    if args.once:
        n = check_once(dry_run=args.dry_run)
        log.info(f"チェック完了: 通知={n}件")
        _pulse(notified=n)
        return

    # 重複ポーリング防止: このコンテナがアイドル指定なら監視せず待機（コンテナは生かす）
    if _is_idle_host():
        host = socket.gethostname()
        log.info(f"[IDLE] このコンテナ（{host}）は idle 指定 → 監視せず待機（監視は別コンテナが担当）")
        try:
            while True:
                _time.sleep(3600)
        except KeyboardInterrupt:
            return

    while True:
        t_start = _time.time()
        n = None
        try:
            n = check_once(dry_run=args.dry_run)
            log.info(f"ポーリング完了: 通知={n}件")
        except Exception as e:
            log.error(f"予期しないエラー: {e}", exc_info=True)
        _pulse(notified=n)   # 生死信号＋日次バックアップ（毎ポーリング・新着0でも打つ）
        _daily_match()       # 1日1回 YouTube候補照合バッチ（ローカルルーティン・前回続き・quota中断）
        elapsed = _time.time() - t_start
        wait = max(0.0, POLL_INTERVAL_SEC - elapsed)
        log.info(f"{int(wait)}秒待機...")
        _time.sleep(wait)

if __name__ == "__main__":
    main()
