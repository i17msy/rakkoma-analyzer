#!/usr/bin/env python3
"""チャンネルのベンチマーク抽出（v1・動くところ優先・精度は後で上げる）。

役割分担（実証済み）:
  再生数(YouTube API) = 「勝ち筋」の審判（客観）
  Gemini(動画解析)    = 「何が違うか」の狙い撃ち特徴抽出（描写は正確・良し悪し判定はしない）
  人間                = どの特徴が効くかのレンズ・最終断定

流れ: 全動画の再生数を取得 → 月別中央値の崖で勝ち筋eraを切り出す
      → 勝ち筋トップ動画を Gemini に渡し formula を抽出 → ベンチマーク・プロファイル出力

使い方: python3 analyze_channel.py <channelId|@handle> [--top 3]
認証  : YOUTUBE_API_KEY（Data API）/ GOOGLE_API_KEY（Gemini）。env か ~/.bashrc から読む。
"""

import collections
import json
import os
import re
import statistics
import sys
from datetime import datetime, timezone

import requests

YT = "https://www.googleapis.com/youtube/v3"
GEMINI = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"


# ── 認証（env か ~/.bashrc・eval不要の堅牢読み）────────────────────────────────

def _key(name: str) -> str | None:
    v = os.environ.get(name)
    if v:
        return v.strip()
    p = os.path.expanduser("~/.bashrc")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8", errors="ignore"):
            m = re.match(rf"\s*export\s+{name}=(['\"]?)(.+?)\1\s*$", line.rstrip())
            if m:
                return m.group(2).strip()
    return None


# ── YouTube: チャンネル解決・全動画＋再生数 ───────────────────────────────────

def _resolve(key: str, ch: str) -> str | None:
    if ch.startswith("UC"):
        return ch
    r = requests.get(f"{YT}/channels", params={
        "key": key, "part": "id", "forHandle": ch.lstrip("@")}, timeout=20).json()
    items = r.get("items", [])
    return items[0]["id"] if items else None


def _all_videos(key: str, cid: str) -> list[dict]:
    up = "UU" + cid[2:]                                   # uploads playlist
    vids, tok = [], None
    while True:
        p = {"key": key, "part": "snippet", "playlistId": up, "maxResults": 50}
        if tok:
            p["pageToken"] = tok
        j = requests.get(f"{YT}/playlistItems", params=p, timeout=20).json()
        for it in j.get("items", []):
            s = it["snippet"]
            vids.append({"id": s["resourceId"]["videoId"],
                         "date": s["publishedAt"][:10], "title": s["title"]})
        tok = j.get("nextPageToken")
        if not tok:
            break
    ids = [v["id"] for v in vids]
    views = {}
    for i in range(0, len(ids), 50):
        j = requests.get(f"{YT}/videos", params={
            "key": key, "part": "statistics", "id": ",".join(ids[i:i + 50])}, timeout=20).json()
        for it in j.get("items", []):
            views[it["id"]] = int(it["statistics"].get("viewCount", 0))
    for v in vids:
        v["views"] = views.get(v["id"], 0)
    vids.sort(key=lambda v: v["date"])
    return vids


def _winning_era(vids: list[dict]):
    """再生数で勝ち筋eraを客観抽出。月別中央値がピークの15%未満に落ちる最初の月=崖。"""
    bym = collections.defaultdict(list)
    for v in vids:
        bym[v["date"][:7]].append(v["views"])
    monthly = {m: int(statistics.median(x)) for m, x in bym.items()}
    if not monthly:
        return {}, 0, None, []
    peak = max(monthly.values())
    cliff = None
    for m in sorted(monthly):
        if m > min(monthly) and peak and monthly[m] < peak * 0.15:
            cliff = m
            break
    win = [v for v in vids if cliff is None or v["date"][:7] < cliff]
    return monthly, peak, cliff, win


# ── Gemini: 勝ち筋動画の formula を狙い撃ち抽出（描写・良し悪し判定はしない）──

_PROMPT = """この動画（あるジャンルの勝ち筋=高再生チャンネルの一本）の「制作formula」を狙い撃ちで抽出してください。
試合内容ではなく"作り方"に注目。良し悪しの判定はせず、何をしているかを具体的に描写:
- hook_type: 冒頭フックの型（感情の一言/数字/挨拶/その他）と実際の文言
- eyecatch: 冒頭のアイキャッチ/コールドオープンの有無と種類
- caption: 字幕デザイン（色・書体・位置・縁取り）
- materials: 使用素材（試合映像/写真/AI画像/CG/グラフ）
- topic_count: 扱うトピック数の体感
- source_stacking: 情報ソースの重ね（数字→メディア→選手→監督→ファン等）をするか
- pacing: 重い情報と軽い情報のメリハリがあるか
- closing: 終わり方（見えた範囲で。定型CTAか）
- formula_summary: この動画が再生を取る"作り"の要点を3文で
日本語JSONのみで返す。"""


def _gemini_formula(gkey: str, vid: str, secs: int = 240) -> dict:
    body = {"contents": [{"parts": [
        {"text": _PROMPT},
        {"file_data": {"file_uri": f"https://www.youtube.com/watch?v={vid}"},
         "video_metadata": {"start_offset": "0s", "end_offset": f"{secs}s"}}]}],
        "generationConfig": {"temperature": 0.2}}
    j = requests.post(f"{GEMINI}?key={gkey}", json=body, timeout=400).json()
    if "candidates" not in j:
        return {"error": j.get("error", j)}
    txt = j["candidates"][0]["content"]["parts"][0]["text"]
    s = txt[txt.find("{"): txt.rfind("}") + 1]
    try:
        return json.loads(s)
    except Exception:
        return {"raw": txt}


# ── メイン ────────────────────────────────────────────────────────────────────

def _save_benchmark(cid, listing_id, win, cliff, monthly, topv, formula):
    """ダッシュボード表示用に channel_benchmark テーブルへ保存（channel_id 主キー）。"""
    import storage
    conn = storage.init()
    conn.execute("""CREATE TABLE IF NOT EXISTS channel_benchmark(
        channel_id TEXT PRIMARY KEY, listing_id TEXT,
        win_start TEXT, win_end TEXT, win_count INTEGER, cliff TEXT,
        monthly_json TEXT, top_videos_json TEXT, formula_json TEXT, fetched_at TEXT)""")
    tv = [{"id": v["id"], "date": v["date"], "views": v["views"], "title": v["title"]} for v in topv]
    conn.execute("INSERT OR REPLACE INTO channel_benchmark VALUES (?,?,?,?,?,?,?,?,?,?)", (
        cid, listing_id,
        win[0]["date"] if win else None, win[-1]["date"] if win else None,
        len(win), cliff,
        json.dumps(monthly, ensure_ascii=False),
        json.dumps(tv, ensure_ascii=False),
        json.dumps(formula, ensure_ascii=False) if formula is not None else None,
        datetime.now(timezone.utc).isoformat()))
    conn.commit()


def run(ch: str, top: int = 3, listing_id: str | None = None) -> int:
    ykey = _key("YOUTUBE_API_KEY")
    if not ykey:
        print("YOUTUBE_API_KEY 未設定（env か ~/.bashrc）")
        return 2
    cid = _resolve(ykey, ch)
    if not cid:
        print(f"チャンネル解決失敗: {ch}")
        return 1

    vids = _all_videos(ykey, cid)
    print(f"■ {ch}  ({cid})  総 {len(vids)} 本")
    monthly, peak, cliff, win = _winning_era(vids)
    print("--- 月別 中央再生数（崖=勝ち筋era終端）---")
    for m in sorted(monthly):
        flag = "  ◀ 崖" if m == cliff else ""
        print(f"  {m}: {monthly[m]:>9,}{flag}")
    if win:
        print(f"勝ち筋era: {win[0]['date']} 〜 {win[-1]['date']}  ({len(win)}本 / 崖={cliff or 'なし'})")

    topv = sorted(win, key=lambda v: -v["views"])[:top]
    print(f"--- 勝ち筋 上位{len(topv)}（再生数）---")
    for v in topv:
        print(f"  {v['views']:>9,} | {v['date']} | {v['title'][:40]}  ({v['id']})")

    formula = None
    gkey = _key("GOOGLE_API_KEY")
    if gkey and topv:
        best = topv[0]
        print(f"\n--- Gemini formula抽出（勝ち筋トップ {best['id']} / {best['views']:,}回・冒頭240秒）---")
        formula = _gemini_formula(gkey, best["id"])
        print(json.dumps(formula, ensure_ascii=False, indent=2))
    elif not gkey:
        print("\nGOOGLE_API_KEY 未設定 → formula抽出スキップ（再生数分析のみ・ダッシュには勝ち筋eraを保存）")

    try:
        _save_benchmark(cid, listing_id, win, cliff, monthly, topv, formula)
    except Exception as e:
        print(f"[warn] ベンチマーク保存失敗: {e}", file=sys.stderr)
    return 0


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    top = 3
    if "--top" in sys.argv:
        top = int(sys.argv[sys.argv.index("--top") + 1])
    if not args:
        print("使い方: python3 analyze_channel.py <channelId|@handle> [--top 3]")
        sys.exit(2)
    sys.exit(run(args[0], top))


if __name__ == "__main__":
    main()
