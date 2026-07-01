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
    views, durs = {}, {}
    for i in range(0, len(ids), 50):                          # statistics＋contentDetails(尺)を同一callで（追加quotaなし）
        j = requests.get(f"{YT}/videos", params={
            "key": key, "part": "statistics,contentDetails", "id": ",".join(ids[i:i + 50])}, timeout=20).json()
        for it in j.get("items", []):
            views[it["id"]] = int(it["statistics"].get("viewCount", 0))
            durs[it["id"]] = _dur_sec(it.get("contentDetails", {}).get("duration", ""))
    for v in vids:
        v["views"] = views.get(v["id"], 0)
        v["dur"] = durs.get(v["id"], 0)
        v["short"] = 0 < v["dur"] <= 60                       # 60秒以下＝Shorts判定（RPMが桁違いに低い）
    vids.sort(key=lambda v: v["date"])
    return vids


def _dur_sec(iso: str) -> int:
    """ISO8601(PT#H#M#S)を秒に。"""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    h, mn, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mn * 60 + s


def _winning_era(vids: list[dict]):
    """再生数で勝ち筋eraを客観抽出。崖=ピーク月より後に月別中央値がピークの15%未満へ落ちる最初の月。
    ※崖はピーク以降に限定する（後でピークが来る成長型chで、ピーク前の低い初月を誤って崖にしないため）。"""
    bym = collections.defaultdict(list)
    for v in vids:
        bym[v["date"][:7]].append(v["views"])
    monthly = {m: int(statistics.median(x)) for m, x in bym.items()}
    if not monthly:
        return {}, 0, None, []
    peak = max(monthly.values())
    peak_m = max(monthly, key=monthly.get)        # ピーク月（同値なら最初）
    cliff = None
    for m in sorted(monthly):
        if m > peak_m and peak and monthly[m] < peak * 0.15:   # 崖はピーク月より後だけ
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


# ── Sonnet 再解釈インサイト（ラッコ↔YouTube実データ・両方ファクト）───────────

_INSIGHT_SYSTEM = """あなたはYouTubeチャンネルM&Aのデューデリ専門家。ラッコ出品(売り手申告)と特定済みのYouTube実データを分析する。
【最重要の前提】
- ラッコ申告(収益/登録者/投稿等)も YouTube実データも、すべて等しく"ファクト"として扱う。
- 申告が虚偽・誇張だ という推論は禁止。一見の矛盾は、両方が事実として両立する自然なメカニズムで説明せよ。
  どんな構造が妥当かは"この案件のデータ"から自分で導くこと。特定の型・言い回し（例の語）に機械的に当てはめない。
- すべてを事実とした上でのみ導ける、買い手に有益な推論だけを出す。各pointは1〜2文で簡潔に。"""

_INSIGHT_TOOL = {
    "name": "submit_insight",
    "description": "買い手向け再解釈インサイト",
    "input_schema": {"type": "object", "properties": {
        "headline": {"type": "string", "description": "案件の本質を一文で"},
        "sections": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "points": {"type": "array", "items": {"type": "string"}}},
            "required": ["title", "points"]},
            "description": "4観点: 実データが語る構造/運営者交代とノウハウ継承/収益の持続性(資産ロングテールvs新規制作力)/買い手の判断材料"},
        "verdict": {"type": "string", "description": "買い手への結論を2〜3文で"}},
        "required": ["headline", "sections", "verdict"]}}


def _channel_stats(key, cid) -> str:
    """現在の登録者数（非公開なら'非公開'）。"""
    try:
        r = requests.get(f"{YT}/channels", params={"key": key, "part": "statistics", "id": cid}, timeout=20).json()
        s = r["items"][0]["statistics"]
        if s.get("hiddenSubscriberCount"):
            return "非公開"
        return f"{int(s.get('subscriberCount', 0)):,}"
    except Exception:
        return "?"


def _sonnet_insight(listing_id, ykey, cid, vids, win, cliff):
    """ラッコ申告とYouTube実データ（両方ファクト）から買い手向け再解釈レポートを生成。"""
    akey = os.environ.get("ANTHROPIC_API_KEY")
    if not akey:
        print("[warn] ANTHROPIC_API_KEY 未設定 → インサイト生成スキップ", file=sys.stderr)
        return None
    import sqlite3
    import storage
    import anthropic
    conn = storage.init()
    conn.row_factory = sqlite3.Row
    L = conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
    if not L:
        return None
    rakkoma = (f"案件名: {L['title']} / 希望価格: {L['price_str']}\n"
               f"登録者(申告): {L['followers']} / 投稿(申告): {L['post_count']} / 運営開始(申告): {L['start_date']}\n"
               f"月次利益(申告): 直近{L['profit_recent']} / 平均{L['profit_avg']} / 最高{L['profit_max']} / 収益モデル: {L['biz_model']}\n"
               f"説明: {(L['description'] or '')[:500]}")
    subs = _channel_stats(ykey, cid)
    bym = collections.defaultdict(list)
    for v in vids:
        bym[v['date'][:7]].append(v['views'])
    traj = " / ".join(f"{m}:{int(statistics.median(bym[m])):,}" for m in sorted(bym))
    last3 = sorted(bym)[-3:]
    rec = [x for m in last3 for x in bym[m]]
    rec_med = int(statistics.median(rec)) if rec else None
    yt = (f"開設(実): {vids[0]['date'] if vids else '?'} / 現登録者(実): {subs} / 総動画(実): {len(vids)}\n"
          f"月別中央再生の推移(実): {traj}\n"
          f"勝ち筋era: {win[0]['date'] if win else '?'}〜{win[-1]['date'] if win else '?'} {len(win)}本"
          f"（崖={cliff}） / 直近中央再生(実): {rec_med}")
    try:
        cli = anthropic.Anthropic(api_key=akey)
        r = cli.messages.create(
            model="claude-sonnet-4-6", max_tokens=2800,
            system=_INSIGHT_SYSTEM, tools=[_INSIGHT_TOOL],
            tool_choice={"type": "tool", "name": "submit_insight"},
            messages=[{"role": "user", "content":
                f"【ラッコ出品(申告=事実)】\n{rakkoma}\n\n【YouTube実データ(特定済み=事実)】\n{yt}"}])
        ins = next(b.input for b in r.content if b.type == "tool_use")
        # 防御: sections が文字列/非リストで返ることがある → 必ず [{title,points}] に正規化
        secs_raw = ins.get("sections")
        if isinstance(secs_raw, str):
            secs_raw = [secs_raw]
        elif not isinstance(secs_raw, list):
            secs_raw = []
        secs = []
        for s in secs_raw:
            if isinstance(s, dict):
                pts = s.get("points")
                secs.append({"title": s.get("title", ""),
                             "points": pts if isinstance(pts, list) else ([str(pts)] if pts else [])})
            else:
                secs.append({"title": "", "points": [str(s)]})
        ins["sections"] = secs
        return ins
    except Exception as e:
        print(f"[warn] Sonnetインサイト生成失敗: {e}", file=sys.stderr)
        return None


def _bias_signals(monthly: dict) -> dict:
    """月別中央再生から再生の時間的偏りを決定論で算出（LLM不要・無料）。"""
    if not monthly:
        return {}
    ms = sorted(monthly)
    peak_m = max(monthly, key=monthly.get)
    peak_v = monthly[peak_m]
    recent = [monthly[m] for m in ms[-3:]]
    recent_med = int(statistics.median(recent)) if recent else 0
    return {"peak_month": peak_m, "peak_view": peak_v,
            "recent_med": recent_med,
            "recent_ratio": round(recent_med / peak_v, 2) if peak_v else None,
            "span": f"{ms[0]}〜{ms[-1]}", "n_months": len(ms)}


# 日本YouTube 長尺AdSense RPM(¥/1000再生)。ジャンルで桁違いなので帯を分ける。上から順にキーワード一致で採用。
_GENRE_RPM = [
    (("金融", "投資", "株式", "仮想通貨", "暗号資産", "不動産", "保険", "節税", "確定申告", "FX", "クレジットカード", "資産運用"), (450, 1100)),
    (("ビジネス", "副業", "マーケティング", "転職", "キャリア", "就活", "法律", "士業", "BtoB", "SaaS"), (300, 700)),
    (("ガジェット", "家電", "PC", "カメラ", "レビュー", "美容", "コスメ", "スキンケア", "健康", "医療", "ダイエット", "脱毛"), (250, 600)),
    (("ゲーム", "エンタメ", "音楽", "アニメ", "漫画", "芸能", "スポーツ", "野球", "サッカー", "格闘", "海外反応", "海外の反応",
      "切り抜き", "朗読", "キッズ", "子供", "ペット", "動物", "反応集", "実況"), (80, 280)),
]
_RPM_DEFAULT = (120, 450)                  # 雑学/まとめ/解説/政治/占い/都市伝説/歴史 等の一般
_SHORT_RPM_LOW, _SHORT_RPM_HIGH = 5, 50    # Shorts（広告プール分配が薄く長尺の1/5〜1/30・ジャンル非依存で低い）


def _genre_rpm(genre: str | None) -> tuple[int, int]:
    """ジャンル文字列から長尺RPM帯を返す（キーワード部分一致・上から優先）。"""
    g = genre or ""
    for kws, band in _GENRE_RPM:
        if any(k in g for k in kws):
            return band
    return _RPM_DEFAULT


def _revenue_check(vids: list, claimed: int | None, genre: str | None = None) -> dict | None:
    """総再生÷運営月数で平均月間再生を出し、RPMレンジで月収を逆算→申告利益と比較（決定論・無料・概算レンジ）。
    RPMは長尺=ジャンル別帯・Shorts=低帯に分け、再生をShorts/長尺に分けて各帯で逆算し合算（混在chも正しく扱う）。
    狙い: ラッコ最重要の申告収益が、実再生で物理的に妥当かのレンジ判定（点でなく幅）。"""
    if not vids or not claimed or claimed <= 0:
        return None
    from datetime import date
    lo, hi = _genre_rpm(genre)                                       # 長尺RPM帯（ジャンル別）
    sv = sum(v.get("views", 0) for v in vids if v.get("short"))      # Shorts再生
    lv = sum(v.get("views", 0) for v in vids if not v.get("short"))  # 長尺再生
    total = sv + lv
    try:                                              # 運営月数=最古動画→今日（実測。申告でなく）
        y, m, d = map(int, vids[0]["date"].split("-"))
        months = max(1.0, (datetime.now(timezone.utc).date() - date(y, m, d)).days / 30.4)
    except Exception:
        months = max(1.0, len(vids) / 4)
    low = int((sv * _SHORT_RPM_LOW + lv * lo) / 1000 / months)
    high = int((sv * _SHORT_RPM_HIGH + lv * hi) / 1000 / months)
    ratio = round((low + high) / 2 / claimed, 2)
    flag = "ok" if 0.6 <= ratio <= 2.0 else ("under" if ratio < 0.6 else "over")
    return {"claimed": int(claimed), "monthly_views": int(total / months),
            "rev_low": low, "rev_high": high, "ratio": ratio, "flag": flag,
            "short_ratio": round(sv / total, 2) if total else 0,
            "rpm_band": [lo, hi]}


def _bias_note(monthly: dict, win: list, cliff, sig: dict, rev: dict | None = None) -> str | None:
    """再生の時間的偏りを1〜2文で簡潔に示唆（Sonnet・短文・約1円）。収益逆算があれば数字に錨を打つ。鍵が無ければNone。"""
    akey = os.environ.get("ANTHROPIC_API_KEY")
    if not akey or not monthly:
        return None
    import anthropic
    traj = " / ".join(f"{m}:{monthly[m]:,}" for m in sorted(monthly))
    ctx = (f"月別中央再生(公開月別): {traj}\n"
           f"ピーク: {sig.get('peak_month')}({sig.get('peak_view'):,}) / "
           f"直近3ヶ月中央: {sig.get('recent_med'):,}(ピーク比 {sig.get('recent_ratio')}) / "
           f"勝ち筋era: {win[0]['date'] if win else '?'}〜{win[-1]['date'] if win else '?'}(崖={cliff})")
    if rev:
        sr = int(rev.get('short_ratio', 0) * 100)
        ctx += (f"\n収益逆算: 平均月間再生{rev['monthly_views']:,}（Shorts比率{sr}%・RPMは長尺/Shortsを分けて算定済）"
                f" → 逆算 ¥{rev['rev_low']:,}〜¥{rev['rev_high']:,}/月 vs 申告 ¥{rev['claimed']:,}/月（比 {rev['ratio']}x）")
    sys_p = ("あなたはYouTubeチャンネルの再生数の時間的偏りを読むアナリスト。月別中央再生(動画の公開月別)を見て、"
             "(1)再生が過去の資産に偏り直近の新作が伸びていないか、(2)ベンチマークするなら着目すべき時期、を"
             "簡潔に1〜2文の日本語で述べよ。注意: 直近月が低いのは新作の再生蓄積が浅いだけの可能性もあるので断定を避け、"
             "ピーク期との差が大きい/勝ち筋eraが過去に固まっている場合のみ『過去資産で延命』と判断する。"
             "収益逆算が与えられ申告が逆算を大きく超える(比<0.6)場合は、過去資産のロングテール依存・別収益源・高RPMニッチ等の"
             "可能性に簡潔に触れてよい（虚偽と断定はしない）。前置き無しで示唆だけ。")
    try:
        cli = anthropic.Anthropic(api_key=akey)
        r = cli.messages.create(model="claude-sonnet-4-6", max_tokens=240,
                                system=sys_p, messages=[{"role": "user", "content": ctx}])
        return "".join(b.text for b in r.content if b.type == "text").strip() or None
    except Exception as e:
        print(f"[warn] 偏り示唆生成失敗: {e}", file=sys.stderr)
        return None


# ── メイン ────────────────────────────────────────────────────────────────────

def _save_benchmark(cid, listing_id, win, cliff, monthly, topv, formula, insight=None, bias_note=None, rev=None):
    """ダッシュボード表示用に channel_benchmark テーブルへ保存（channel_id 主キー）。"""
    import storage
    conn = storage.init()
    conn.execute("""CREATE TABLE IF NOT EXISTS channel_benchmark(
        channel_id TEXT PRIMARY KEY, listing_id TEXT,
        win_start TEXT, win_end TEXT, win_count INTEGER, cliff TEXT,
        monthly_json TEXT, top_videos_json TEXT, formula_json TEXT, fetched_at TEXT,
        insight_json TEXT)""")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(channel_benchmark)")}
    if "insight_json" not in cols:                                   # 既存テーブルへの追加
        conn.execute("ALTER TABLE channel_benchmark ADD COLUMN insight_json TEXT")
    if "bias_note" not in cols:                                      # 再生偏り示唆（軽量）
        conn.execute("ALTER TABLE channel_benchmark ADD COLUMN bias_note TEXT")
    if "rev_json" not in cols:                                       # 収益逆算（決定論）
        conn.execute("ALTER TABLE channel_benchmark ADD COLUMN rev_json TEXT")
    tv = [{"id": v["id"], "date": v["date"], "views": v["views"], "title": v["title"]} for v in topv]
    conn.execute("""INSERT OR REPLACE INTO channel_benchmark
        (channel_id, listing_id, win_start, win_end, win_count, cliff,
         monthly_json, top_videos_json, formula_json, fetched_at, insight_json, bias_note, rev_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        cid, listing_id,
        win[0]["date"] if win else None, win[-1]["date"] if win else None,
        len(win), cliff,
        json.dumps(monthly, ensure_ascii=False),
        json.dumps(tv, ensure_ascii=False),
        json.dumps(formula, ensure_ascii=False) if formula is not None else None,
        datetime.now(timezone.utc).isoformat(),
        json.dumps(insight, ensure_ascii=False) if insight is not None else None,
        bias_note,
        json.dumps(rev, ensure_ascii=False) if rev is not None else None))
    conn.commit()


def run(ch: str, top: int = 3, listing_id: str | None = None, deep: bool = False) -> int:
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

    # 収益逆算（決定論・無料）: 申告利益が実再生で物理的に妥当かのレンジ判定。listing_id があれば申告利益を読む
    rev = None
    if listing_id:
        try:
            import storage as _st
            _c = _st.init()
            _r = _c.execute("""SELECT l.profit_avg, l.profit_recent, e.genre
                FROM listings l LEFT JOIN evaluations e ON e.listing_id=l.id WHERE l.id=?""", (listing_id,)).fetchone()
            claimed = (_r[0] if _r and _r[0] else (_r[1] if _r else None))
            genre = _r[2] if _r else None
            rev = _revenue_check(vids, claimed, genre)
            if rev:
                f = {"under": "⚠️", "over": "🔼", "ok": "✅"}[rev["flag"]]
                print(f"--- 収益逆算 {f} ---\n  申告 ¥{rev['claimed']:,}/月 vs 再生逆算 ¥{rev['rev_low']:,}〜¥{rev['rev_high']:,}/月 "
                      f"（平均月間再生{rev['monthly_views']:,} / RPM帯¥{rev['rpm_band'][0]}〜{rev['rpm_band'][1]}[{genre or '一般'}] / 比 {rev['ratio']}x）")
        except Exception as e:
            print(f"[warn] 収益逆算スキップ: {e}", file=sys.stderr)

    # 再生の時間的偏り示唆（決定論シグナル＋Sonnet一言・約1円）— 軽量の既定インサイト。収益逆算も食わせて数字に錨を打つ
    sig = _bias_signals(monthly)
    bias = _bias_note(monthly, win, cliff, sig, rev)
    if bias:
        print(f"\n--- 再生の偏り示唆 ---\n  {bias}")

    # 掘り下げ＝Sonnet 再解釈インサイト（ラッコ↔YouTube実データを両方ファクトとして再解釈）
    # ※Gemini動画formula抽出は将来の「本気でレシピ化」段階のオプションに格下げ（今は呼ばない）
    # 重いSonnet再解釈レポートは既定OFF（--deep 指定時のみ・約$0.022）。既定は偏り示唆(約1円)まで
    insight = None
    if listing_id and deep:
        print(f"\n--- Sonnet 再解釈インサイト（ラッコ申告 × YouTube実データ・両方ファクト）---")
        insight = _sonnet_insight(listing_id, ykey, cid, vids, win, cliff)
        if insight:
            print("◆", insight.get("headline", ""))
            for s in insight.get("sections", []):
                print(f"\n■ {s.get('title', '')}")
                for p in s.get("points", []):
                    print(f"  ・{p}")
            print("\n◆ VERDICT:", insight.get("verdict", ""))
    elif listing_id:
        print("\n（再解釈レポートは既定OFF — 偏り示唆のみ保存。--deep で生成）")
    else:
        print("\n（listing_id 未指定 → 勝ち筋era＋偏り示唆のみ保存）")

    try:
        _save_benchmark(cid, listing_id, win, cliff, monthly, topv, formula=None, insight=insight, bias_note=bias, rev=rev)
    except Exception as e:
        print(f"[warn] ベンチマーク保存失敗: {e}", file=sys.stderr)
    return 0


def export_urls(ch: str, frm: str | None = None, to: str | None = None, out: str | None = None) -> int:
    """指定chの動画URLを期間指定で .txt 一覧出力（Whisperバッチ等の入口＝受け渡しパッケージ）。
    期間未指定なら勝ち筋eraを既定範囲にする。日付は YYYY-MM か YYYY-MM-DD（両端含む）。"""
    ykey = _key("YOUTUBE_API_KEY")
    if not ykey:
        print("YOUTUBE_API_KEY 未設定（env か ~/.bashrc）")
        return 2
    cid = _resolve(ykey, ch)
    if not cid:
        print(f"チャンネル解決失敗: {ch}")
        return 1
    vids = _all_videos(ykey, cid)
    if not frm and not to:                                # 既定＝勝ち筋era
        _m, _p, _cliff, win = _winning_era(vids)
        if win:
            frm, to = win[0]["date"], win[-1]["date"]
        print(f"（期間未指定 → 勝ち筋era {frm}〜{to} を既定範囲に）")

    def _in(d):
        if frm and d < frm:
            return False
        if to and d[:len(to)] > to:                       # to が YYYY-MM なら月単位で両端含む
            return False
        return True
    sel = sorted((v for v in vids if _in(v["date"])), key=lambda v: v["date"])
    if not sel:
        print("該当動画なし（期間を確認）")
        return 0
    expdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "exports")
    os.makedirs(expdir, exist_ok=True)
    path = out or os.path.join(expdir, f"{cid}_{frm or 'start'}_{to or 'end'}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(f"https://www.youtube.com/watch?v={v['id']}" for v in sel) + "\n")
    print(f"■ {ch} ({cid}) / 範囲 {frm or '最初'}〜{to or '最後'} / {len(sel)}本")
    print(f"→ {path}")
    for v in sel:
        print(f"  {v['date']}  https://www.youtube.com/watch?v={v['id']}  {v['title'][:42]}")
    return 0


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    def _flag(name):
        return sys.argv[sys.argv.index(name) + 1] if name in sys.argv and sys.argv.index(name) + 1 < len(sys.argv) else None
    if not args:
        print("使い方: python3 analyze_channel.py <channelId|@handle> [--top 3] [--listing <案件ID>] [--deep]\n"
              "        python3 analyze_channel.py <channelId|@handle> --urls [--from YYYY-MM] [--to YYYY-MM] [--out path.txt]")
        sys.exit(2)
    if "--urls" in sys.argv:                              # 動画URL一覧を期間指定で .txt 出力
        sys.exit(export_urls(args[0], _flag("--from"), _flag("--to"), _flag("--out")))
    top = int(_flag("--top")) if _flag("--top") else 3
    listing_id = _flag("--listing")
    deep = "--deep" in sys.argv          # 重い再解釈レポートを生成（既定OFF・約$0.022）
    sys.exit(run(args[0], top, listing_id=listing_id, deep=deep))


if __name__ == "__main__":
    main()
