#!/usr/bin/env python3
"""YouTube候補照合（半自動・v1=候補提示まで）。

ラッコ案件の匿名データ（ジャンル/登録者数/投稿数/開設時期）から、YouTube上の
"似たチャンネル"候補を確率的に探し、近似順に N 件提示する。最終断定は人間（断定/補完はv2）。

これは決定論的な"特定"ではなく確率的な候補照合。匿名化でch名が落ちているため、
ジャンル語での検索プールに実チャンネルが現れるかが recall の限界（多くは低信頼 or 出ない）。
出力は「信頼度付き候補リスト」で、"特定不能/曖昧"を一級の結果として扱う。

認証: read-only な公開データ取得（search.list / channels.list）のみ → OAuth不要・APIキーで可。
  YT_API_KEY（env）または data/yt_api_key（gitignore・1行）から読む。
  ※uploadするMLB側がOAuthなのは書き込みだから。こちらは読むだけなのでキーで足りる。

使い方: python3 match.py <案件ID> [--n 5]
"""

import json
import math
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(__file__))
import storage
from config import DATA_DIR, ANALYZER_MODEL

YT = "https://www.googleapis.com/youtube/v3"
KEY_FILE = DATA_DIR / "yt_api_key"


# ── 認証・入出力 ──────────────────────────────────────────────────────────────

def _api_key() -> str | None:
    k = os.environ.get("YOUTUBE_API_KEY") or os.environ.get("YT_API_KEY")
    if k:
        return k.strip()
    if KEY_FILE.exists():
        return KEY_FILE.read_text(encoding="utf-8").strip() or None
    return None


def _load_listing(conn, lid: str) -> dict | None:
    r = conn.execute(
        "SELECT id, title, category, description, followers, post_count, start_date "
        "FROM listings WHERE id=?", (lid,)).fetchone()
    if not r:
        return None
    cols = ["id", "title", "category", "description", "followers", "post_count", "start_date"]
    d = dict(zip(cols, r))
    d["followers"] = _int(d["followers"])      # DBに文字列で入る事があるので数値強制
    d["post_count"] = _int(d["post_count"])
    return d


def _int(v):
    if v is None or isinstance(v, int):
        return v
    s = re.sub(r"[^\d]", "", str(v))
    return int(s) if s else None


# ── 検索クエリ生成（LLM・recallの肝）──────────────────────────────────────────

def _gen_queries(listing: dict) -> list[str]:
    """説明文から「視聴者が使う」日本語検索クエリを1〜3本生成。鍵が無ければ素朴版。"""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        try:
            import anthropic
            cli = anthropic.Anthropic(api_key=key)
            prompt = (
                "次はYouTubeチャンネルのM&A案件（匿名化済み・チャンネル名は不明）です。"
                "このチャンネルを検索で見つけるための日本語クエリを4〜6個。\n"
                "重要: 広さに幅をつけること。①ジャンルを表す素直な2〜3語の組合せ"
                "（例「MLB 翻訳 解説」「シニア 暮らし」）と ②もう少し具体的な語、の両方を混ぜる。\n"
                "視聴者が実際に打つ自然な語にし、チャンネル名は推測しない。"
                "専門的すぎる語（チャンネルに無いかもしれない語）は避け、素直なジャンル語を優先。\n\n"
                f"案件名: {listing.get('title')}\nカテゴリ: {listing.get('category')}\n"
                f"説明: {(listing.get('description') or '')[:800]}\n\n"
                'JSON のみで返す: {"queries": ["...", "..."]}'
            )
            resp = cli.messages.create(
                model=ANALYZER_MODEL, max_tokens=400,
                messages=[{"role": "user", "content": prompt}])
            txt = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            txt = txt[txt.find("{"): txt.rfind("}") + 1]
            qs = json.loads(txt).get("queries", [])
            qs = [q.strip() for q in qs if isinstance(q, str) and q.strip()]
            if qs:
                return qs[:6]
        except Exception as e:
            print(f"[warn] LLMクエリ生成失敗→素朴版: {e}", file=sys.stderr)
    # 素朴版フォールバック: カテゴリ＋案件名から記号を除いた語
    base = f"{listing.get('category') or ''} {listing.get('title') or ''}"
    for ch in "【】[]（）()｜|・/":
        base = base.replace(ch, " ")
    return [" ".join(base.split()[:6])]


# ── YouTube Data API（read-only・APIキー）─────────────────────────────────────

class _QuotaExceeded(Exception):
    """YouTube Data API のクォータ上限。バッチはこれを捕えて安全に中断する。"""


def _yt_search(key: str, q: str, n: int = 50) -> list[str]:
    r = requests.get(f"{YT}/search", params={
        "key": key, "part": "snippet", "type": "channel", "q": q,
        "maxResults": min(n, 50), "regionCode": "JP", "relevanceLanguage": "ja",
    }, timeout=20)
    if r.status_code == 403 and "quota" in r.text.lower():
        raise _QuotaExceeded(q)
    r.raise_for_status()
    return [it["id"]["channelId"] for it in r.json().get("items", [])
            if it.get("id", {}).get("channelId")]


def _yt_channels(key: str, ids: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(ids), 50):
        r = requests.get(f"{YT}/channels", params={
            "key": key, "part": "snippet,statistics,topicDetails",
            "id": ",".join(ids[i:i + 50]),
        }, timeout=20)
        r.raise_for_status()
        for it in r.json().get("items", []):
            st = it.get("statistics", {})
            sn = it.get("snippet", {})
            out.append({
                "id": it["id"],
                "title": sn.get("title"),
                "subs": None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0) or 0),
                "videos": int(st.get("videoCount", 0) or 0),
                "published": (sn.get("publishedAt") or "")[:7],   # YYYY-MM
                "country": sn.get("country"),
                "desc": sn.get("description", ""),
                "topics": it.get("topicDetails", {}).get("topicCategories", []),
            })
    return out


# ── スコアリング（透明・複数同時一致で信頼度が立つ）───────────────────────────

def _prox(a, b) -> float | None:
    """正の2値の対数近接 0..1（10倍ズレ≈0.35）。片方欠損→None。"""
    if not a or not b:
        return None
    r = abs(math.log10(max(b, 1) / max(a, 1)))
    return round(1 / (1 + r) ** 1.5, 3)


def _age_score(start_date, published) -> float | None:
    """運営開始(粗)と開設月の近さ。"""
    import re
    m = re.search(r"(\d{4})\D{0,3}(\d{1,2})?", start_date or "")
    if not m or not published:
        return None
    y0, mo0 = int(m.group(1)), int(m.group(2) or 6)
    try:
        y1, mo1 = int(published[:4]), int(published[5:7])
    except ValueError:
        return None
    months = abs((y0 - y1) * 12 + (mo0 - mo1))
    return round(1 / (1 + months / 12), 3)


def _topic_score(listing, cand) -> float | None:
    """案件のジャンル/語が候補の title/desc に現れる度合い（素朴）。"""
    import re
    base = f"{listing.get('category') or ''} {listing.get('title') or ''}"
    toks = [t for t in re.split(r"[\s【】\[\]（）()｜|・/、,]+", base) if len(t) >= 2]
    if not toks:
        return None
    hay = f"{cand.get('title') or ''} {cand.get('desc') or ''}".lower()
    hit = sum(1 for t in set(toks) if t.lower() in hay)
    return round(min(1.0, hit / max(3, len(set(toks)) * 0.5)), 3)


# 登録者・投稿は具体的な硬い数字。開始時期はラッコ自己申告で誤りが出る（実測で1年ズレを確認）
# ため弱める＝"誤メタデータに一致するオトリ"を持ち上げない。topicは現状ほぼ無情報（弱め）。
_W = {"subs": 0.45, "videos": 0.33, "age": 0.10, "topic": 0.12}


def _score(listing, cand) -> dict:
    parts = {
        "subs": _prox(listing.get("followers"), cand.get("subs")),
        "videos": _prox(listing.get("post_count"), cand.get("videos")),
        "age": _age_score(listing.get("start_date"), cand.get("published")),
        "topic": _topic_score(listing, cand),
    }
    num = sum(_W[k] * v for k, v in parts.items() if v is not None)
    den = sum(_W[k] for k, v in parts.items() if v is not None)
    conf = round(num / den, 3) if den else 0.0
    return {"confidence": conf, "parts": parts}


def _video_thumbs(key: str, cid: str, n: int = 10) -> list[str]:
    """候補chの直近n本の動画サムネURL（uploads playlist = UU+ID）。設計を視覚スキャンする用。"""
    up = "UU" + cid[2:]
    try:
        r = requests.get(f"{YT}/playlistItems", params={
            "key": key, "part": "snippet", "playlistId": up, "maxResults": n}, timeout=20)
        if r.status_code == 403 and "quota" in r.text.lower():
            raise _QuotaExceeded("thumbs")
        if r.status_code != 200:
            return []
        out = []
        for it in r.json().get("items", []):
            th = it["snippet"].get("thumbnails", {})
            u = (th.get("medium") or th.get("default") or {}).get("url")
            if u:
                out.append(u)
        return out
    except _QuotaExceeded:
        raise
    except Exception:
        return []


# ── 永続化（ダッシュ描画/断定用）──────────────────────────────────────────────

def _save(conn, lid, scored, key):
    conn.execute("""CREATE TABLE IF NOT EXISTS channel_candidates(
        listing_id TEXT, channel_id TEXT, channel_title TEXT,
        subs INTEGER, videos INTEGER, published TEXT,
        confidence REAL, breakdown_json TEXT, fetched_at TEXT,
        status TEXT DEFAULT 'candidate', thumbs_json TEXT,
        PRIMARY KEY(listing_id, channel_id))""")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(channel_candidates)")}
    if "thumbs_json" not in cols:                                  # 既存テーブルへ追加
        conn.execute("ALTER TABLE channel_candidates ADD COLUMN thumbs_json TEXT")
    conn.execute("DELETE FROM channel_candidates WHERE listing_id=? AND status='candidate'", (lid,))
    now = datetime.now(timezone.utc).isoformat()
    for c, s in scored:
        thumbs = _video_thumbs(key, c["id"])                       # 各候補の直近動画サムネ
        conn.execute("""INSERT OR REPLACE INTO channel_candidates
            (listing_id, channel_id, channel_title, subs, videos, published,
             confidence, breakdown_json, fetched_at, status, thumbs_json)
            VALUES (?,?,?,?,?,?,?,?,?, 'candidate', ?)""",
            (lid, c["id"], c["title"], c["subs"], c["videos"], c["published"],
             s["confidence"], json.dumps(s["parts"], ensure_ascii=False), now,
             json.dumps(thumbs, ensure_ascii=False)))
    conn.commit()


# ── メイン ────────────────────────────────────────────────────────────────────

def run(lid: str, n: int = 5, benchmark: bool = False, regen: bool = True, deep: bool = False) -> int:
    key = _api_key()
    if not key:
        print("YT_API_KEY 未設定（env か data/yt_api_key）。READMEの手順でAPIキーを用意してください。")
        return 2
    conn = storage.init()
    listing = _load_listing(conn, lid)
    if not listing:
        print(f"案件 {lid} がDBに見つかりません。")
        return 1

    print(f"■ 案件 {lid}: {listing['title']}")
    print(f"  ラッコ: 登録者={listing['followers']} / 投稿={listing['post_count']} / 開始={listing['start_date']} / {listing['category']}")

    queries = _gen_queries(listing)
    print(f"  検索クエリ: {queries}")

    ids = []
    for q in queries:
        try:
            ids += _yt_search(key, q)
        except _QuotaExceeded:
            raise                                        # バッチで安全中断するため握りつぶさない
        except Exception as e:
            print(f"[warn] 検索失敗 '{q}': {e}", file=sys.stderr)
    ids = list(dict.fromkeys(ids))                       # 重複除去・順序維持
    if not ids:
        print("  → 候補なし（検索にヒットせず＝特定不能の可能性）")
        return 0

    cands = _yt_channels(key, ids)
    scored = sorted(((c, _score(listing, c)) for c in cands),
                    key=lambda cs: -cs[1]["confidence"])[:n]
    _save(conn, lid, scored, key)

    print(f"\n  候補 上位{len(scored)}（近似順・信頼度は弱い指紋の合成。最終判断は人間）:")
    for i, (c, s) in enumerate(scored, 1):
        p = s["parts"]
        sub = f"{c['subs']}" if c["subs"] is not None else "非公開"
        print(f"  {i}. [{int(s['confidence']*100):3d}%] {c['title']}")
        print(f"       登録者 {sub}（案件 {listing['followers']}）/ 投稿 {c['videos']}（案件 {listing['post_count']}）/ 開設 {c['published']}")
        print(f"       内訳 subs={p['subs']} videos={p['videos']} age={p['age']} topic={p['topic']}")
        print(f"       https://www.youtube.com/channel/{c['id']}")
    print("\n  ※ 登録者・投稿が同時に近い候補ほど確からしい。1指標だけ一致は低信頼。")

    # --benchmark: 筆頭候補をそのままベンチマーク抽出まで連結（案件ID一発）
    if benchmark and scored:
        top = scored[0][0]
        print(f"\n{'='*64}")
        print(f"▼ 筆頭候補「{top['title']}」を自動ベンチマーク抽出（勝ち筋era＋偏り示唆{'＋再解釈レポート' if deep else ''}）")
        print(f"  ※ 筆頭が誤りなら: python3 analyze_channel.py <正しいchID> --listing {lid}{' --deep' if deep else ''} で再実行")
        print(f"{'='*64}")
        import analyze_channel
        analyze_channel.run(top["id"], top=3, listing_id=lid, deep=deep)

    # 候補保存後にダッシュボードを再生成（照合→即ダッシュ反映＝一気通貫）
    # ※バッチ時は regen=False（最後に1回だけまとめて再生成する）
    if regen:
        try:
            import dashboard
            print()
            dashboard.main()
        except Exception as e:
            print(f"[warn] ダッシュ再生成失敗: {e}", file=sys.stderr)
    return 0


def batch(limit: int = 19, bench: bool = True) -> int:
    """募集中×買い/様子見×適合≥4×YouTube種別 の未検索案件を一括候補検索（サムネ視覚スキャン用に母数を広げる）。
    既定で筆頭候補の軽いベンチ（勝ち筋era＋偏り示唆・~1円/件）も付与（重い再解釈レポートは無し）。
    quota上限で安全中断（再実行で続きから）。"""
    conn = storage.init()
    searched = {r[0] for r in conn.execute("SELECT DISTINCT listing_id FROM channel_candidates")}
    rows = conn.execute("""
        SELECT l.id FROM listings l JOIN evaluations e ON e.listing_id=l.id
        WHERE l.status_state='募集中' AND e.verdict IN ('買い','様子見')
          AND e.capability_fit>=4 AND l.asset_type LIKE '%YouTube%'
        ORDER BY e.overall_score DESC""").fetchall()
    targets = [str(r[0]) for r in rows if str(r[0]) not in searched][:limit]
    print(f"=== 一括候補検索（買い/様子見×適合≥4×YouTube×未検索）対象 {len(targets)} 件 / 上限{limit}"
          f"{' ＋筆頭ベンチ(偏り示唆)' if bench else ''} ===")
    done = 0
    for i, lid in enumerate(targets, 1):
        print(f"\n──────── [{i}/{len(targets)}] 案件 {lid} ────────")
        try:
            run(lid, benchmark=bench, regen=False, deep=False)
            done += 1
        except _QuotaExceeded:
            print(f"\n[STOP] YouTube quota 上限に到達。{done}件で中断（明日 --batch 再実行で続きから）", file=sys.stderr)
            break
        except Exception as e:
            print(f"[warn] 案件{lid} 失敗: {e}", file=sys.stderr)
    try:
        import dashboard
        dashboard.main()                                  # 最後に1回だけ
    except Exception as e:
        print(f"[warn] ダッシュ再生成失敗: {e}", file=sys.stderr)
    print(f"\n=== 完了: {done}/{len(targets)} 件 検索（残りは再実行で続行）===")
    return 0


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--batch" in sys.argv:
        limit = 19
        if "--limit" in sys.argv:
            limit = int(sys.argv[sys.argv.index("--limit") + 1])
        sys.exit(batch(limit))
    n = 5
    if "--n" in sys.argv:
        n = int(sys.argv[sys.argv.index("--n") + 1])
    benchmark = "--benchmark" in sys.argv
    deep = "--deep" in sys.argv          # 重い再解釈レポートも生成（既定OFF・約$0.022）
    if not args:
        print("使い方: python3 match.py <案件ID> [--n 5] [--benchmark] [--deep]  /  python3 match.py --batch [--limit 19]")
        sys.exit(2)
    sys.exit(run(args[0], n, benchmark=benchmark, deep=deep))


if __name__ == "__main__":
    main()
