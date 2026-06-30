#!/usr/bin/env python3
"""data/listings/*.json から静的ダッシュボード data/dashboard.html を生成。

LLM評価（再現性重視）+ 定量メトリクスを一覧化。総合スコア順ソート・列ヘッダで
並べ替え・行クリックで強み/弱み/再現メモを展開。サーバー不要、ブラウザで開くだけ。

  python3 dashboard.py
"""

import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import DASHBOARD_FILE
import storage as DB

JST = timezone(timedelta(hours=9))


def load() -> list[dict]:
    return DB.fetch_dashboard_rows(DB.init())


def _load_youtube(conn):
    """match.py / analyze_channel.py が貯めた候補ch・ベンチマークを読む（無ければ空）。"""
    import sqlite3
    cands, bench = {}, {}
    try:
        for r in conn.execute(
            "SELECT listing_id, channel_id, channel_title, subs, videos, published, confidence, thumbs_json, fetched_at "
            "FROM channel_candidates WHERE status='candidate' ORDER BY confidence DESC"):
            cands.setdefault(r[0], []).append({
                "channel_id": r[1], "title": r[2], "subs": r[3],
                "videos": r[4], "published": r[5], "confidence": r[6],
                "thumbs": json.loads(r[7]) if r[7] else [], "fetched_at": r[8]})
    except sqlite3.OperationalError:
        pass
    try:
        cur = conn.execute("SELECT * FROM channel_benchmark")  # column-name方式（insight_json有無に頑健）
        names = [d[0] for d in cur.description]
        for row in cur.fetchall():
            d = dict(zip(names, row))
            bench[d["channel_id"]] = {
                "listing_id": d.get("listing_id"),   # レポートは生成元の案件でのみ表示する
                "win_start": d.get("win_start"), "win_end": d.get("win_end"),
                "win_count": d.get("win_count"), "cliff": d.get("cliff"),
                "top_videos": json.loads(d.get("top_videos_json") or "[]"),
                "bias_note": d.get("bias_note"),
                "rev": json.loads(d["rev_json"]) if d.get("rev_json") else None,
                "insight": json.loads(d["insight_json"]) if d.get("insight_json") else None}
    except sqlite3.OperationalError:
        pass
    return cands, bench


# ジャンル表示の短縮辞書（表示専用・長いキーから順に置換）。自由に追加可。
GENRE_ABBR = [
    ("LINEリスト誘導型サービス販売", "LINEリスト誘導"),
    ("キュレーション", "キュレ"),
    ("スピリチュアル", "スピ"),
    ("ナレーション", "ナレ"),
    ("アフィリエイト", "アフィ"),
    ("人材系サービス", "人材系"),
]


def abbr_genre(s: str) -> str:
    for a, b in GENRE_ABBR:
        s = s.replace(a, b)
    return s


HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ラッコM&A アナライザー</title>
<style>
  :root { --bg:#03050b; --panel:#1a2029; --line:#202733; --fg:#e6edf3; --mut:#8b98a9;
          --good:#3fb950; --mid:#d29922; --bad:#f85149; --accent:#58a6ff; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font-family:-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif; font-size:14px; }
  header { padding:16px 22px; border-bottom:1px solid var(--line); display:flex;
           align-items:baseline; gap:14px; flex-wrap:wrap; }
  header h1 { font-size:18px; margin:0; }
  header .meta { color:var(--mut); font-size:12px; }
  .controls { padding:10px 22px; display:flex; gap:8px; flex-wrap:wrap; align-items:center;
              border-bottom:1px solid var(--line); position:sticky; top:0; z-index:30; background:var(--bg); }
  .controls input, .controls select { background:var(--panel); color:var(--fg);
              border:1px solid var(--line); border-radius:6px; padding:6px 9px; font-size:12px; }
  .controls button { background:var(--panel); color:var(--fg); border:1px solid var(--line);
              border-radius:6px; padding:6px 11px; font-size:12px; cursor:pointer; }
  .controls button:hover { border-color:var(--accent); }
  .controls .sp { flex:1; }
  table { width:100%; border-collapse:collapse; }
  th, td { padding:9px 10px; text-align:left; border-bottom:1px solid var(--line);
           white-space:nowrap; }
  th { color:var(--mut); font-weight:600; cursor:pointer; user-select:none; position:sticky;
       top:var(--ctrlh,52px); z-index:20; background:var(--bg); font-size:12.5px; }
  th:hover { color:var(--fg); }
  th.num, td.num { text-align:right; font-variant-numeric:tabular-nums; }
  th.ctr, td.ctr { text-align:center; }
  td.flags { font-size:17px; letter-spacing:1px; white-space:nowrap; }
  td.mom { font-size:19px; font-weight:700; }
  tr.row { cursor:pointer; }
  tr.row:hover > td { background:#0a0e15; }
  tr.row.open > td { background:#13283d; }
  tr.row.open > td:first-child { box-shadow: inset 3px 0 0 var(--accent); }
  td.title { max-width:420px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  td.title a { color:var(--accent); text-decoration:none; }
  td.title a:hover { text-decoration:underline; }
  .pill { display:inline-block; padding:2px 8px; border-radius:999px; font-size:11px;
          font-weight:600; }
  .v-buy   { background:rgba(63,185,80,.16); color:var(--good); }
  .v-watch { background:rgba(210,153,34,.16); color:var(--mid); }
  .v-pass  { background:rgba(248,81,73,.14); color:var(--bad); }
  .v-none  { background:#262d38; color:var(--mut); }
  .st-open { background:rgba(88,166,255,.16); color:var(--accent); }
  .st-sold { background:rgba(63,185,80,.14); color:var(--good); }
  .st-with { background:#262d38; color:var(--mut); }
  .score { font-weight:700; }
  .s-hi { color:var(--good); } .s-mid { color:var(--mid); } .s-lo { color:var(--bad); }
  .genre { color:var(--mut); }
  .bmstar { cursor:pointer; color:#56657a; margin-right:8px; font-size:18px; user-select:none; }
  .bmstar:hover { color:#f4d03f; } .bmstar.on { color:#f4d03f; }
  .dbmstar { font-size:26px; margin-right:11px; vertical-align:middle; }
  .gtag { display:inline-block; max-width:140px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
          vertical-align:middle; background:#16212e; border:1px solid #294056; color:#aecbe2;
          border-radius:11px; padding:1px 10px; font-size:13.5px; }
  .detail td { background:#0d121b; padding:0; white-space:normal; }
  .detail .inner { padding:16px 20px 20px; display:grid; grid-template-columns:1fr 1fr; gap:20px;
                   font-size:14.5px; border-left:3px solid var(--accent); position:relative; }
  .closeBtn { position:absolute; top:2px; right:12px; background:transparent; border:none;
              color:var(--mut); font-size:52px; line-height:1; cursor:pointer; padding:2px 14px; }
  .closeBtn:hover { color:var(--fg); }
  .detail h4 { margin:0 0 7px; font-size:16px; color:#9fb4cc; text-transform:uppercase;
               letter-spacing:.04em; }
  .detail ul { margin:0; padding-left:18px; } .detail li { margin:4px 0; line-height:1.6; }
  .detail .full { grid-column:1 / -1; }
  .detail .note { background:var(--panel); border:1px solid var(--line); border-radius:8px;
                  padding:11px 13px; line-height:1.7; }
  .detail .note .pill { font-size:13px; padding:2px 9px; }
  .bars { display:flex; gap:16px; flex-wrap:wrap; align-items:baseline; }
  .bar { font-size:16px; } .bar b { color:var(--fg); }
  .dtitle { color:var(--accent); font-size:24px; font-weight:600; text-decoration:none; }
  .dtitle:hover { text-decoration:underline; }
  .dtitlebar { position:sticky; top:calc(var(--ctrlh,52px) + 33px); z-index:6; background:#0d121b;
               padding:8px 0 9px; margin:-2px 0 3px; border-bottom:1px solid var(--line); }
  .drecap { display:flex; flex-wrap:wrap; align-items:center; gap:7px 14px; margin-top:7px; font-size:15px; }
  .drecap .rc { color:#c2cfdd; } .drecap .rc b { font-weight:700; font-size:17px; margin-left:2px; }
  .bars.scores { gap:22px; padding-bottom:6px; border-bottom:1px solid var(--line); }
  .scores .bar { font-size:18px; } .scores b { font-size:23px; font-weight:700; }
  .scores b.s-hi { color:var(--good); } .scores b.s-mid { color:var(--mid); } .scores b.s-lo { color:var(--bad); }
  .bar b.s-lo { color:var(--bad); } .bar b.s-mid { color:var(--mid); } .bar b.s-hi { color:var(--good); }
  .freshbar { padding:5px 10px; border-radius:6px; background:#1a2029; display:inline-block; font-size:14px; }
  .detail h4.hStr { color:var(--good); } .detail h4.hWeak { color:var(--mid); }
  .ytsec { background:#0a1119; border:1px solid #25405a; border-left:3px solid #c4302b;
           border-radius:8px; padding:0 14px; margin-top:6px; }
  .ytsec[open] { padding-bottom:13px; }
  .ytsec > summary { color:#ff7a6b; font-size:22px; font-weight:700; cursor:pointer;
           padding:11px 0 9px; list-style:none; outline:none; }
  .ytsec > summary::-webkit-details-marker { display:none; }
  .ytsec > summary::before { content:'▸'; color:#7fb1e0; margin-right:9px; }
  .ytsec[open] > summary::before { content:'▾'; }
  .ytc { padding:9px 2px; border-top:1px solid #1b2937; line-height:1.65; font-size:15px; }
  .ytc:first-of-type { border-top:none; }
  .ytcline { font-size:19px; }
  .ytcline > b { display:inline-block; min-width:56px; font-size:23px; font-weight:700; }
  .ytage { font-size:19px; }
  .ytcline a { color:#6db3f2; margin:0 9px; text-decoration:none; } .ytcline a:hover { text-decoration:underline; }
  .ytstrip { display:flex; flex-wrap:wrap; gap:5px; margin:8px 0 2px; }
  .ytth { height:80px; width:auto; border-radius:4px; border:1px solid #1c2a3a; display:block; }
  .ytera { color:#9fc6ef; font-size:16px; margin:8px 0 0 2px; }
  .ytrev { color:#c6d2de; font-size:16px; margin:5px 0 0 2px; } .ytrev b { font-weight:700; }
  .rev-ok { color:#7fd6a0; } .rev-warn { color:#e2a04a; } .rev-bad { color:#e2493f; }
  .ytrev-none { color:#e2705a; } .ytrev-none b { color:#e2705a; font-weight:700; }
  .ytbias { color:#ecdcae; background:#181a10; border-left:3px solid #c9a84a; border-radius:5px;
            padding:8px 12px; margin:7px 0 2px; font-size:16.5px; line-height:1.6; }
  .report { margin:7px 0 3px 54px; background:#0e1a26; border:1px solid #1c3145; border-radius:7px; }
  .report > summary { cursor:pointer; padding:9px 12px; color:#ffd27a; font-weight:700;
            font-size:15.5px; line-height:1.55; list-style:none; outline:none; }
  .report > summary::-webkit-details-marker { display:none; }
  .report > summary::before { content:'▸'; color:#7fb1e0; margin-right:8px; }
  .report[open] > summary::before { content:'▾'; }
  .report[open] > summary { border-bottom:1px solid #1c3145; color:#ffe3a8; }
  .rbody { padding:10px 13px 13px; }
  .ytb { margin:0 0 9px; font-size:14.5px; color:#a9c2dc; }
  .insight { font-size:15.5px; line-height:1.7; }
  .insec { margin:0 0 12px; }
  .institle { color:#7fd1c0; font-weight:700; margin:0 0 4px; font-size:15.5px; }
  .insight ul { margin:0; padding-left:19px; } .insight li { margin:5px 0; color:#d4dfea; }
  .inverdict { background:#1b1408; border-left:3px solid #ffb347; border-radius:6px;
               padding:11px 14px; margin-top:6px; color:#eddfc4; font-size:15.5px; line-height:1.7; }
  .mut { color:var(--mut); }
  .hidden { display:none; }
  .axis { display:inline-block; min-width:42px; }
  /* 総合パネル（アコーディオン・2列グリッド） */
  .overview { background:#0e1722; border:1px solid #1d2c3d; border-radius:10px;
              margin:10px 4px 14px; padding:8px 10px; }
  .ov-grid2 { display:grid; grid-template-columns:1fr 1fr 1fr; gap:7px 10px; align-items:start; }
  @media (max-width:1200px){ .ov-grid2 { grid-template-columns:1fr 1fr; } }
  @media (max-width:760px){ .ov-grid2 { grid-template-columns:1fr; } }
  .ov-sec { border:1px solid #1a2839; border-radius:8px; margin:0; background:#0b1320; overflow:hidden; }
  .ov-sec-h { display:flex; align-items:baseline; gap:9px; padding:9px 12px; cursor:pointer; user-select:none; }
  .ov-sec-h:hover { background:#101d2c; }
  .ov-chev { color:#5f7da0; font-size:14px; width:14px; }
  .ov-sec-t { font-size:17.5px; font-weight:700; color:#cdddf0; }
  .ov-sec-teaser { margin-left:auto; color:#8aa3bd; font-size:15px; text-align:right; }
  .ov-sec.open .ov-sec-h { border-bottom:1px solid #16263a; }
  .ov-sec-b { padding:8px 16px 13px; }
  .ov-row { display:flex; justify-content:space-between; align-items:baseline; gap:10px;
            padding:5px 0; font-size:17px; border-top:1px solid #121e2b; }
  .ov-row:first-child { border-top:none; }
  .ov-row .k { color:#b3c2d4; font-size:15px; }
  .ov-big { font-size:24px; font-weight:700; color:#7fd6a0; }
  .ov-tag { display:inline-block; background:#15273a; border:1px solid #24405c; color:#bcd4ee;
            border-radius:11px; padding:2px 11px; margin:3px 5px 3px 0; font-size:15px; }
  .ov-note { color:#6f8298; font-size:14px; margin-top:9px; line-height:1.55; }
  .ov-hi { color:#7fd6a0; } .ov-lo { color:#e2a04a; }
  /* 円/ドーナツグラフ（conic-gradient・ライブラリ不要） */
  .ov-chart { display:flex; gap:54px; align-items:center; justify-content:center; margin:5px 0 12px; }
  .ov-pie { width:42%; max-width:260px; aspect-ratio:1; height:auto; border-radius:50%; flex:0 0 auto; }
  .ov-donut { -webkit-mask:radial-gradient(circle, transparent 54%, #000 55%);
                      mask:radial-gradient(circle, transparent 54%, #000 55%); }
  /* 凡例は固定幅(基準)にして、内容が変わっても「円＋凡例」の総幅＝中央位置を不変に保つ */
  .ov-leg { display:flex; flex-direction:column; gap:9px; font-size:20px; color:#aab8c8; flex:0 0 38%; }
  .ov-leg div { white-space:nowrap; }
  .ov-leg i { display:inline-block; width:13px; height:13px; border-radius:3px; margin-right:10px; vertical-align:middle; }
  .ov-leg b { color:#e8eef6; font-weight:700; margin:0 13px 0 17px; }
  .ov-leg .k { margin-left:2px; }
</style>
</head>
<body>
<header>
  <h1>🦦 ラッコM&A アナライザー</h1>
  <span class="meta">再現性重視評価 · <span id="cnt"></span>件 · 生成 __GENERATED__</span>
</header>
<div class="controls">
  <input id="q" placeholder="ID・タイトル・ジャンル検索…" oninput="render()">
  <select id="atf" onchange="render()">
    <option value="__yt" selected>YouTube</option>
    <option value="">種別: すべて</option>
    <option value="__nonyt">その他</option>
  </select>
  <select id="sf" onchange="render()">
    <option value="">状態: すべて</option>
    <option value="募集中">募集中</option>
    <option value="__closed">クローズ</option>
    <option value="成約済み">成約済み</option>
    <option value="受付終了">受付終了</option>
  </select>
  <select id="pf" onchange="render()">
    <option value="">価格: すべて</option>
    <option value="0-300000">〜30万</option>
    <option value="300000-500000">30〜50万</option>
    <option value="500000-1000000">50〜100万</option>
    <option value="1000000-3000000">100〜300万</option>
    <option value="3000000-10000000">300〜1000万</option>
    <option value="10000000-">1000万〜</option>
  </select>
  <select id="capOp" onchange="render()">
    <option value="">適合: すべて</option>
    <option value="ge">適合 ≥</option>
    <option value="eq">適合 =</option>
    <option value="le">適合 ≤</option>
  </select>
  <select id="capVal" onchange="render()">
    <option>1</option><option>2</option><option>3</option><option selected>4</option><option>5</option>
  </select>
  <select id="vf" onchange="render()">
    <option value="">判定: すべて</option>
    <option value="買い">買い</option>
    <option value="様子見">様子見</option>
    <option value="見送り">見送り</option>
    <option value="__none">未評価</option>
  </select>
  <select id="ff" onchange="render()">
    <option value="">フラグ: すべて</option>
    <option value="__ytdone">🎥 YT検索済み</option>
    <option value="__ytnone">— YT未検索</option>
    <option value="__clean">✨クリーン(系列✅)</option>
    <option value="__none">フラグなし(系列✖)</option>
    <option value="__any">フラグあり</option>
    <option value="交渉">🎯 交渉ターゲット</option>
    <option value="急成長">🚀 急成長×ピーク</option>
    <option value="ピーク売り">🔝 ピーク売り</option>
    <option value="立上げ初期">🌱 立上げ初期</option>
    <option value="高変動">⚡ 高変動</option>
    <option value="停止復活">⛔ 停止復活歴</option>
    <option value="下降">📉 下降トレンド</option>
    <option value="運営乖離">⚠️ 運営乖離</option>
    <option value="収益不明">❓ 収益不明(系列✖)</option>
    <option value="実績安定">✅ 実績安定</option>
  </select>
  <label class="mut"><input type="checkbox" id="evalOnly" onchange="render()"> 評価済みのみ</label>
  <label class="mut"><input type="checkbox" id="bmOnly" onchange="render()"> ★のみ <span id="bmcnt" class="mut"></span></label>
  <button id="bmExportBtn" onclick="bmExport()" title="★をbookmarks.jsonに書き出し">⬇</button>
  <button id="toggleAll" onclick="toggleAllRows()">▼ 全展開</button>
  <button id="ovBtn" onclick="toggleOverview()">📊 総合</button>
  <span class="sp"></span>
  <span class="meta mut" id="fcnt"></span>
</div>
<div id="overview" class="overview" hidden></div>
<table>
  <thead><tr id="head"></tr></thead>
  <tbody id="body"></tbody>
</table>

<script>
const DATA = __DATA__;
const _dbBookmarks = __BOOKMARKS__;   // DB(bookmarksテーブル)由来の★。localStorageと統合する＝マシン跨ぎ
const COLS = [
  {k:'title',  label:'案件',     get:r=>r.title||'', cls:'title'},
  {k:'state',  label:'状態',     get:r=>r.status_state ?? null, statePill:true, align:'center'},
  {k:'cap',    label:'適合',     get:r=>r.evaluation?.capability_fit ?? null, num:true, s5:true, align:'center'},
  {k:'overall',label:'総合',     get:r=>r.evaluation?.overall_score ?? null, num:true, score:true, align:'center'},
  {k:'verdict',label:'判定',     get:r=>r.evaluation?.verdict ?? null, align:'center'},
  {k:'ytmatch',label:'YT',       get:r=>(r.candidates&&r.candidates.length)?r.candidates[0].confidence:null, num:true, align:'center'},
  {k:'flags',  label:'🚩',       get:r=>r.flags||[], cls:'flags', align:'center'},
  {k:'mom',    label:'勢',       get:r=>r.metrics?.stability ?? null, cls:'mom', align:'center'},
  {k:'profit', label:'平均利益', get:r=>r.metrics?.profit_avg ?? r.profit ?? null, num:true, money:true},
  {k:'price',  label:'価格',     get:r=>r.price ?? null, num:true, money:true},
  {k:'payback',label:'回収月',   get:r=>r.metrics?.payback_months_recent ?? null, num:true},
  {k:'operating',label:'運営',   get:r=>r.operating_months ?? null, num:true},
  {k:'stale',  label:'滞留',     get:r=>r.dwell_days ?? null, num:true},
  {k:'settled', label:'成約',    get:r=>r.settled_at ?? null, cls:'date', align:'right'},
  {k:'listed', label:'掲載',     get:r=>r.listed_at ?? null, cls:'date', align:'right'},
  {k:'genre',  label:'ジャンル', get:r=>r.evaluation?.genre_main||'', cls:'genre'},
];
let sortKey='cap', sortDir=-1;   // 既定: 適合(降順) → 総合(降順)
const VRANK={'買い':0,'様子見':1,'見送り':2};
function keyVal(c,r){ return c.k==='verdict' ? (VRANK[r.evaluation?.verdict] ?? 9) : c.get(r); }
function acl(c){ return c.align==='center'?'ctr':((c.align==='right'||c.num)?'num':''); }

const yen = n => n==null ? '–' : '¥'+Number(n).toLocaleString();
function sCls(v,max){ if(v==null) return ''; const r=v/max; return r>=0.7?'s-hi':r>=0.45?'s-mid':'s-lo'; }
function vPill(v){
  const m={'買い':'v-buy','様子見':'v-watch','見送り':'v-pass'};
  return v ? `<span class="pill ${m[v]}">${v}</span>` : `<span class="pill v-none">未評価</span>`;
}
function stPill(v){
  const m={'募集中':'st-open','成約済み':'st-sold','受付終了':'st-with'};
  return v ? `<span class="pill ${m[v]||'v-none'}">${v}</span>` : '<span class="mut">–</span>';
}
function flagIcon(f){
  if(f.startsWith('交渉'))       return '🎯';
  if(f.startsWith('立上げ初期')) return '🌱';
  if(f.startsWith('急成長'))     return '🚀';
  if(f.startsWith('ピーク売り')) return '🔝';
  if(f.startsWith('高変動'))     return '⚡';
  if(f.startsWith('停止復活'))   return '⛔';
  if(f.startsWith('下降'))       return '📉';
  if(f.startsWith('運営乖離'))   return '⚠️';
  if(f.startsWith('実績安定'))   return '✅';
  if(f.startsWith('収益不明'))   return '❓';
  if(f.startsWith('収益ゼロ'))   return '🚫';
  return '🏷';
}
const FLAG_PRIORITY=['交渉','収益不明','収益ゼロ','運営乖離','停止復活','急成長','ピーク売り','立上げ初期','高変動','下降','実績安定'];
function flagRank(f){ for(let i=0;i<FLAG_PRIORITY.length;i++) if(f.startsWith(FLAG_PRIORITY[i])) return i; return 99; }
function flagCell(fl){
  if(!fl || !fl.length) return '<span class="mut">–</span>';
  const s=[...fl].sort((a,b)=>flagRank(a)-flagRank(b));
  const names=s.map(f=>f.replace('急成長×ピーク売り抜け','急成長×ピーク'));
  return `<span title="${esc(names.join(' / '))}">${s.map(flagIcon).join('')}</span>`;
}
// 履歴インサイト: 運営/収益化/立上げ/乖離 を1つのライフサイクル判定に合成
function lifecycle(op, mm, gap){
  if(gap!=null && gap>=3)            return {ic:'⚠️', label:'移管疑い', cls:'s-lo'};
  if(op==null || mm==null)           return {ic:'',   label:'不明',     cls:'mut'};
  if(op<=6)                          return {ic:'🌱', label:'新規',     cls:'s-mid'};
  if(op<=18 && mm<12)                return {ic:'📈', label:'成長期',   cls:''};
  if(mm>=12 && op>=24)               return {ic:'🏛', label:'成熟',     cls:'s-hi'};
  return {ic:'✓', label:'確立', cls:'s-hi'};
}
function cell(c,r){
  let v=c.get(r);
  if(c.cls==='title'){
    const u=r.url||'#';
    return `<a href="${u}" target="_blank" rel="noopener">${esc(v)}</a>`;
  }
  if(c.statePill) return stPill(v);
  if(c.k==='flags') return flagCell(v);  // 🎥は廃止(YT列の一致率と完全重複のため)
  if(c.k==='ytmatch') return v==null?'<span class="mut">–</span>':`<span class="${ytConf(v)}">${Math.round(v*100)}%</span>`;
  if(c.k==='verdict') return vPill(v);
  if(v==null) return '<span class="mut">–</span>';
  if(c.money) return yen(v);
  if(c.score) return `<span class="score ${sCls(v,5)}">${v}</span>`;
  if(c.s5)    return `<span class="${sCls(v,5)}">${v}</span>`;
  if(c.k==='payback') return v+'ヶ月';
  if(c.k==='mom'){ const a = v>=1.15?['↗','s-hi'] : v<0.85?['↘','s-lo'] : ['→','mut'];
                   return `<span class="${a[1]}" title="勢い x${v}（直近÷平均）">${a[0]}</span>`; }
  if(c.k==='operating') return (v/12).toFixed(1)+'年';
  if(c.k==='gap') return v>=3 ? `<span class="s-lo">+${v}</span>` : '<span class="mut">–</span>';
  if(c.k==='stale'){
    const k=r.dwell_kind;
    const tip=k==='sold'?'掲載→成約までの期間':k==='open'?'掲載→現在（募集中・進行中）':k==='ended'?'掲載→更新日の概算（終了日は非公開）':'';
    return k==='ended' ? `<span class="mut" title="${tip}">~${v}日</span>` : `<span title="${tip}">${v}日</span>`;
  }
  if(c.k==='deal') return v+'日';
  if(c.k==='genre'){ if(!v) return ''; return `<span class="gtag" title="${esc(r.evaluation?.genre||'')}">${esc(v)}</span>`; }
  return esc(String(v));
}
function esc(s){ return (s||'').replace(/[&<>"]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m])); }

function ytConf(v){ return v>=0.7?'s-hi':v>=0.5?'s-mid':'s-lo'; }
// 収益逆算の比→段階(色/絵文字/意味)。極端は候補誤りを疑う
function _revTier(x){
  if(x<0.1||x>5)  return {e:'🚩', c:'rev-bad',  t:'候補誤り疑い'};
  if(x<0.6)       return {e:'⚠️', c:'rev-warn', t:'資産依存'};
  if(x<=2.0)      return {e:'✅', c:'rev-ok',   t:'妥当'};
  return                 {e:'🔼', c:'rev-warn', t:'申告控えめ'};   // 2.0<x≤5
}
function reportBlock(b){
  if(!b) return '';
  const era=`<div class="ytb">📐 勝ち筋era <b>${b.win_start||'?'}〜${b.win_end||'?'}</b>（${b.win_count}本・崖${b.cliff||'-'}）</div>`;
  const ins=b.insight;
  if(!ins){  // 掘り下げ未実施＝勝ち筋eraのみ（折りたたまずそのまま）
    return `<div class="report"><div class="rbody">${era}<span class="mut">（未掘り下げ — match.py --benchmark で再解釈レポート生成）</span></div></div>`;
  }
  const secs=(ins.sections||[]).map(s=>{
    const title=(s&&s.title)||'';
    const pts=(s&&Array.isArray(s.points))?s.points:(typeof s==='string'?[s]:[]);
    return `<div class="insec">${title?`<div class="institle">${esc(title)}</div>`:''}<ul>${pts.map(p=>`<li>${esc(p)}</li>`).join('')}</ul></div>`;
  }).join('');
  return `<details class="report"><summary>📋 詳細レポート — ${esc(ins.headline||'')}</summary>`
    +`<div class="rbody">${era}<div class="insight">${secs}`
    +(ins.verdict?`<div class="inverdict"><b>総評</b> ${esc(ins.verdict)}</div>`:'')
    +`</div></div></details>`;
}
function ytSection(cands){
  // レポートは一旦非表示（データはDBに温存）。サムネで設計を視覚スキャンする方を主役に。
  const items=cands.map(c=>{
    const subs=(c.subs==null)?'非公開':Number(c.subs).toLocaleString();
    const thumbs=(c.thumbs||[]).map(u=>`<img class="ytth" loading="lazy" src="${esc(u)}">`).join('');
    let age='';
    if(c.fetched_at){ const d=Math.floor((Date.now()-new Date(c.fetched_at).getTime())/86400000);
      age=` <span class="mut ytage">· 📷${d<=0?'今日取得':d+'日前取得'}</span>`; }
    let bench='';
    if(c.benchmark){ const b=c.benchmark;
      let rev='';
      if(b.rev){ const rv=b.rev; const t=_revTier(rv.ratio);
        const sr=Math.round((rv.short_ratio||0)*100);
        const smark=sr>=30?` <span class="mut" title="Shorts比率${sr}%（RPMは長尺¥120〜450/Shorts¥5〜50で分けて逆算済）">⚡Shorts${sr}%</span>`:'';
        rev=`<div class="ytrev" title="再生をShorts/長尺に分け各RPM(長尺¥120〜450・Shorts¥5〜50/千再生)で逆算した概算レンジ。比=逆算中央÷申告。極端な比は筆頭候補の誤マッチを疑う">`
          +`💰 申告 ${yen(rv.claimed)}/月 vs 再生逆算 ${yen(rv.rev_low)}〜${yen(rv.rev_high)}/月 <b class="${t.c}">${t.e} ${rv.ratio}x （${t.t}）</b>${smark}</div>`; }
      else { rev=`<div class="ytrev ytrev-none" title="ラッコに月次の申告利益が開示されていないため逆算できません（収益条件達成と謳いつつ数字非開示＝注意）">💰 <b>申告利益の開示なし（逆算不可）</b></div>`; }
      bench=`<div class="ytera">📐 勝ち筋era <b>${b.win_start||'?'}〜${b.win_end||'?'}</b>（${b.win_count}本・崖${b.cliff||'-'}）</div>`
        +rev+(b.bias_note?`<div class="ytbias">📊 ${esc(b.bias_note)}</div>`:''); }
    return `<div class="ytc"><div class="ytcline">`
      +`<b class="${ytConf(c.confidence)}">${Math.round(c.confidence*100)}%</b>`
      +`<a href="https://www.youtube.com/channel/${c.channel_id}" target="_blank" rel="noopener">${esc(c.title)}</a>`
      +`<span class="mut">登録${subs} / 投稿${c.videos} / 開設${c.published||'-'}</span>${age}</div>`
      +bench+(thumbs?`<div class="ytstrip">${thumbs}</div>`:'')+`</div>`;
  }).join('');
  return `<details class="full ytsec" open><summary>🎥 YouTube候補（近似順・サムネで設計を見る・${cands.length}件）</summary>${items}</details>`;
}
function detailRow(r,span){
  const e=r.evaluation, m=r.metrics||{};
  if(!e){
    return `<tr class="detail hidden"><td colspan="${span}"><div class="inner">
      <button class="closeBtn" onclick="closeDetail(event,this)" title="閉じる">×</button>
      <div class="full mut">未評価。<code>python3 analyze.py --id ${r.id}</code> で評価できます。</div>
      </div></td></tr>`;
  }
  const li=a=>(Array.isArray(a)?a:[]).map(x=>`<li>${esc(x)}</li>`).join('');
  // 収益化表示と"真の立ち上げ期間"(運営−収益化, 窓が未飽和=収益化<12 のときのみ算出可能)
  const _mm=m.monetized_months, _op=r.operating_months;
  const monStr = _mm==null ? '–' : (_mm>=12 ? '12ヶ月+' : _mm+'ヶ月');
  const ramp = (_mm!=null && _mm<12 && _op!=null) ? Math.max(0, _op-_mm) : null;
  const lc = lifecycle(_op, _mm, r.history_gap);
  const opStr = _op==null ? '–' : (_op/12).toFixed(1)+'年';
  const histSub = `運営${opStr} / 収益化${monStr}${ramp?` / 立上げ${ramp}ヶ月`:''}`;
  const _age=r.data_age_months, _closed=r.status_state!=='募集中';
  const _ac=_age==null?'':(_age>=6?'s-lo':_age>=3?'s-mid':'s-hi');
  const _fresh=(_closed||(_age!=null&&_age>=4))
    ? `<div class="full"><span class="freshbar mut">📅 ${_closed?'過去データ(売買当時)':'データ更新'} <b class="${_ac}">${(r.updated_at||r.listed_at||'?')}時点 ・ ${_age==null?'?':_age+'ヶ月前'}</b>${_closed?' — 直近/勢いは当時の断面':''}</span></div>`
    : '';
  return `<tr class="detail hidden"><td colspan="${span}"><div class="inner">
    <button class="closeBtn" onclick="closeDetail(event,this)" title="閉じる">×</button>
    <div class="full dtitlebar"><span class="bmstar dbmstar${isBm(r.id)?' on':''}" onclick="bmToggle('${r.id}',event)" title="ブックマーク">${isBm(r.id)?'★':'☆'}</span><a href="${r.url||'#'}" target="_blank" rel="noopener" class="dtitle">${esc(r.title||'')}</a>
      <div class="drecap">${stPill(r.status_state)}`
      +(e?.capability_fit!=null?` <span class="rc">適合<b class="${sCls(e.capability_fit,5)}">${e.capability_fit}</b></span>`:'')
      +(e?.overall_score!=null?` <span class="rc">総合<b class="${sCls(e.overall_score,5)}">${e.overall_score}</b></span>`:'')
      +(e?.verdict?` ${vPill(e.verdict)}`:'')
      +(r.price!=null?` <span class="rc">${yen(r.price)}</span>`:'')
      +(m.payback_months_recent!=null?` <span class="rc">回収<b>${m.payback_months_recent}</b>ヶ月</span>`:'')
      +`</div></div>
    ${_fresh}
    <div class="full bars scores">
      <span class="bar mut">ID <b>${r.id}</b></span>
      <span class="bar">${flagCell(r.flags)}</span>
      <span class="bar mut">総合 <b class="score ${sCls(e.overall_score,5)}">${e.overall_score}</b></span>
      <span class="bar mut">再現 <b class="${sCls(e.scores.replicability,5)}">${e.scores.replicability}</b></span>
      <span class="bar mut">持続 <b class="${sCls(e.scores.sustainability,5)}">${e.scores.sustainability}</b></span>
      <span class="bar mut">割安 <b class="${sCls(e.scores.value,5)}">${e.scores.value}</b></span>
      <span class="bar mut">成長 <b class="${sCls(e.scores.growth,5)}">${e.scores.growth}</b></span>
      <span class="bar mut">勢い <b class="${m.stability==null?'':(m.stability>=1?'s-hi':m.stability>=0.7?'s-mid':'s-lo')}">${m.stability==null?'–':'x'+m.stability}</b></span>
      <span class="bar mut">リスク係数 <b class="${(e.risk_factor!=null&&e.risk_factor<1)?'s-lo':''}">${e.risk_factor==null?'–':'x'+e.risk_factor}</b></span>
    </div>
    <div><h4 class="hStr">強み</h4><ul>${li(e.strengths)}</ul></div>
    <div><h4 class="hWeak">弱み・リスク</h4><ul>${li(e.weaknesses)}</ul></div>
    <div class="full"><h4>再現メモ（自分で作るなら）</h4><div class="note">${esc(e.replication_note)}</div></div>
    <div class="full"><h4>判定理由</h4><div class="note">${vPill(e.verdict)} ${esc(e.verdict_reason)}</div></div>
    <div class="full bars">
      <span class="bar mut">履歴 <b class="${lc.cls}" style="font-size:15px;margin:0 12px">${lc.ic} ${lc.label}</b> <span class="mut" style="margin-left:6px">${histSub}</span></span>
      <span class="bar mut">CV(変動) <b>${m.cv ?? '–'}</b></span>
      <span class="bar mut">トレンド <b>${m.trend==null?'–':m.trend+'%'}</b></span>
      <span class="bar mut">直近/最高 <b>${m.recent_vs_max ?? '–'}</b></span>
    </div>
    <div class="full bars">
      <span class="bar mut">登録者 <b>${esc(r.followers_str||'–')}</b></span>
      <span class="bar mut">平均利益 <b>${yen(m.profit_avg)}</b></span>
      <span class="bar mut">直近月利益 <b>${yen(m.profit_recent)}</b></span>
      <span class="bar mut">最低/最高 <b class="${m.profit_min===0?'s-lo':''}">${yen(m.profit_min)}</b> / <b>${yen(m.profit_max)}</b></span>
      <span class="bar mut">登録者1k人あたり利益 <b>${yen(m.profit_per_1k_subs)}</b></span>
      <span class="bar mut">収益モデル <b>${esc(r.biz_model||'–')}</b></span>
    </div>
    ${r.candidates ? ytSection(r.candidates) : ''}
  </div></td></tr>`;
}

function render(){
  const q=document.getElementById('q').value.trim().toLowerCase();
  const capOp=document.getElementById('capOp').value;
  const capVal=+document.getElementById('capVal').value;
  const vf=document.getElementById('vf').value;
  const ff=document.getElementById('ff').value;
  const sf=document.getElementById('sf').value;
  const atf=document.getElementById('atf').value;
  const pf=document.getElementById('pf').value;
  const evalOnly=document.getElementById('evalOnly').checked;
  const bmOnly=document.getElementById('bmOnly').checked;
  // asset_type が一件も埋まっていない間(バックフィル中)は種別フィルタを無効化して空表示を防ぐ
  const hasAT=DATA.some(r=>r.asset_type);

  let rows=DATA.filter(r=>{
    if(evalOnly && !r.evaluation) return false;
    if(bmOnly && !isBm(r.id)) return false;
    if(atf==='__yt' && hasAT && !(r.asset_type||'').includes('YouTube')) return false;
    if(atf==='__nonyt' && (r.asset_type||'').includes('YouTube')) return false;
    if(pf){ const ps=pf.split('-'); const lo=+ps[0], hi=ps[1]===''?null:+ps[1];
      if(r.price==null) return false;
      if(r.price<lo) return false;
      if(hi!=null && r.price>=hi) return false; }
    if(sf){
      if(sf==='__closed'){ if(r.status_state!=='成約済み' && r.status_state!=='受付終了') return false; }
      else if(r.status_state!==sf) return false;
    }
    if(vf==='__none' && r.evaluation) return false;
    if(vf && vf!=='__none' && r.evaluation?.verdict!==vf) return false;
    if(ff){
      const fl=r.flags||[];
      const ytdone=!!(r.candidates&&r.candidates.length);
      const hasrep=!!(r.candidates&&r.candidates.some(x=>x.benchmark&&x.benchmark.insight));
      if(ff==='__ytdone' && !ytdone) return false;
      else if(ff==='__ytnone' && ytdone) return false;
      else if(ff==='__report' && !hasrep) return false;
      else if(ff==='__none' && (fl.length || r.metrics?.monetized_months!=null)) return false;
      else if(ff==='__clean' && (fl.length || r.metrics?.monetized_months==null)) return false;
      else if(ff==='__any' && !fl.length) return false;
      else if(!['__none','__any','__clean','__ytdone','__ytnone','__report'].includes(ff) && !fl.some(f=>f.startsWith(ff))) return false;
    }
    if(capOp){
      const cf=r.evaluation?.capability_fit;
      if(cf==null) return false;
      if(capOp==='ge' && !(cf>=capVal)) return false;
      if(capOp==='eq' && !(cf===capVal)) return false;
      if(capOp==='le' && !(cf<=capVal)) return false;
    }
    if(q){ const hay=(String(r.id)+' '+(r.title||'')+' '+(r.evaluation?.genre||'')).toLowerCase();
           if(!hay.includes(q)) return false; }
    return true;
  });

  const col=COLS.find(c=>c.k===sortKey);
  if(col) rows.sort((a,b)=>{                          // sortKey=null(3回目)は並べ替えず既定順を維持
    let x=keyVal(col,a), y=keyVal(col,b);
    const xn=x==null, yn=y==null;
    if(xn&&!yn) return 1; if(yn&&!xn) return -1;       // null は常に最下部
    if(!xn&&!yn){
      if(typeof x!=='number'){ x=String(x); y=String(y); }
      if(x<y) return -sortDir; if(x>y) return sortDir;
    }
    // 2段目: 総合の降順
    return (b.evaluation?.overall_score ?? -Infinity)-(a.evaluation?.overall_score ?? -Infinity);
  });

  document.getElementById('cnt').textContent=DATA.length;
  document.getElementById('fcnt').textContent=`表示 ${rows.length.toLocaleString()} 件`;
  _lastRows=rows;
  if(!document.getElementById('overview').hidden) renderOverview(rows);
  document.getElementById('head').innerHTML=COLS.map(c=>{
    const ar=sortKey===c.k?(sortDir<0?' ▾':' ▴'):'';
    return `<th class="${acl(c)}" onclick="sortBy('${c.k}')">${c.label}${ar}</th>`;
  }).join('');

  const span=COLS.length;
  document.getElementById('body').innerHTML=rows.map((r,i)=>{
    const tds=COLS.map(c=>`<td class="${acl(c)} ${c.cls||''}">${cell(c,r)}</td>`).join('');
    return `<tr class="row" onclick="toggle(this)">${tds}</tr>`+detailRow(r,span);
  }).join('');
  // 行を作り直したので全展開状態はリセット
  _allOpen=false; const _ta=document.getElementById('toggleAll'); if(_ta) _ta.textContent='▼ 全展開';
}
function sortBy(k){
  const first=(k==='verdict')?1:-1;                // その列の初回方向(通常=降順 / 判定=昇順)
  if(sortKey!==k){ sortKey=k; sortDir=first; }      // 1回目: 初回方向
  else if(sortDir===first){ sortDir=-first; }       // 2回目: 逆方向
  else { sortKey=null; sortDir=-1; }                // 3回目: ソート無し(既定の並び)
  render();
}
function toggle(tr){ const d=tr.nextElementSibling; const opening=d.classList.contains('hidden'); d.classList.toggle('hidden'); tr.classList.toggle('open', opening); }
function closeDetail(ev,btn){ ev.stopPropagation(); const d=btn.closest('tr.detail'); d.classList.add('hidden'); if(d.previousElementSibling) d.previousElementSibling.classList.remove('open'); }

// フィルター行(sticky)の実高さを測り、列見出し/案件名のstick位置をその下に合わせる
function setCtrlH(){ const c=document.querySelector('.controls');
  if(c) document.documentElement.style.setProperty('--ctrlh', c.offsetHeight+'px'); }

// 表示中(フィルタ後)の案件を一括 開く/閉じる
let _allOpen=false;
function _expandAll(open){
  document.querySelectorAll('#body tr.row').forEach(tr=>{
    const d=tr.nextElementSibling;
    if(d&&d.classList.contains('detail')){ d.classList.toggle('hidden',!open); tr.classList.toggle('open',open); }
  });
}
function toggleAllRows(){
  if(!_allOpen){
    const n=document.querySelectorAll('#body tr.row').length;
    if(n>60 && !confirm(n+'件を全部開きます。重くなる可能性があります。続けますか？')) return;
  }
  _allOpen=!_allOpen; _expandAll(_allOpen);
  document.getElementById('toggleAll').textContent=_allOpen?'▲ 全閉じる':'▼ 全展開';
}

// ===== 総合パネル（表示中=フィルタ後の行を集計）=====
let _lastRows=[];
let _ovOpen={compose:true,fit:true,speed:true,match:true,yt:true,quality:true};   // 既定で1〜2行目を開く（新並び順）
function toggleOverview(){
  const el=document.getElementById('overview'); el.hidden=!el.hidden;
  document.getElementById('ovBtn').textContent=el.hidden?'📊 総合':'📊 総合 ✕';
  if(!el.hidden) renderOverview(_lastRows);
}
function ovToggleSec(id){ _ovOpen[id]=!_ovOpen[id]; renderOverview(_lastRows); }
function _med(a){ if(!a.length) return null; const s=[...a].sort((x,y)=>x-y); const m=s.length>>1;
  return s.length%2?s[m]:Math.round((s[m-1]+s[m])/2); }
function _pct(a,b){ return b?Math.round(a/b*100):0; }
function _ovRow(k,v,big){ return `<div class="ov-row"><span class="k">${esc(k)}</span><span${big?' class="ov-big"':''}>${v}</span></div>`; }
function _ovSec(id,title,teaser,body){
  const op=!!_ovOpen[id];
  return `<div class="ov-sec ${op?'open':''}"><div class="ov-sec-h" onclick="ovToggleSec('${id}')">`
    +`<span class="ov-chev">${op?'▾':'▸'}</span><span class="ov-sec-t">${esc(title)}</span>`
    +`<span class="ov-sec-teaser">${teaser}</span></div>`
    +(op?`<div class="ov-sec-b">${body}</div>`:'')+`</div>`;
}
function _toks(rows){ const t={};
  rows.forEach(r=>{ const g=r.evaluation?.genre; if(!g) return;
    g.split(/[\/、・,\s]+/).forEach(w=>{ w=w.trim(); if(w.length>=2) t[w]=(t[w]||0)+1; }); });
  return Object.entries(t).sort((a,b)=>b[1]-a[1]); }
function _tags(arr,n){ return arr.slice(0,n).map(t=>`<span class="ov-tag">${esc(t[0])} ${t[1]}</span>`).join(''); }
function _pie(segs,donut){ const tot=segs.reduce((s,x)=>s+(x.v||0),0); if(!tot) return '';
  let acc=0; const stops=segs.map(x=>{ const a=acc/tot*100, b=(acc+(x.v||0))/tot*100; acc+=(x.v||0); return `${x.c} ${a}% ${b}%`; });
  return `<span class="ov-pie${donut?' ov-donut':''}" style="background:conic-gradient(${stops.join(',')})"></span>`; }
function _chart(segs,donut){ const tot=segs.reduce((s,x)=>s+(x.v||0),0); if(!tot) return '';
  const leg=segs.filter(x=>x.v>0).map(x=>`<div><i style="background:${x.c}"></i>${esc(x.t)} <b>${x.v}</b> <span class="k">${_pct(x.v,tot)}%</span></div>`).join('');
  return `<div class="ov-chart">${_pie(segs,donut)}<div class="ov-leg">${leg}</div></div>`; }
// 強→弱の順序ランプ（隣接で色相を大きく離す）。緑=強・赤=弱
const _RAMP5=['#27ae60','#9acd32','#f4d03f','#ef8b32','#db4a4a'];  // 5段(5→1)
const _RAMP4=['#27ae60','#9acd32','#ef8b32','#db4a4a'];            // 4段(強→弱)
const _CBUY='#27ae60', _CWATCH='#f4d03f', _CPASS='#8d98a6', _CNONE='#33404e';  // 判定
const _COPEN='#3d8edd', _CSOLD='#27ae60', _CEND='#8d98a6', _CDARK='#22303f';   // 状態/未充填

function renderOverview(rows){
  const el=document.getElementById('overview'); if(el.hidden) return;
  const n=rows.length;
  const stc=s=>rows.filter(r=>r.status_state===s).length;
  const open=stc('募集中'), sold=stc('成約済み'), ended=stc('受付終了');
  const ev=rows.filter(r=>r.evaluation);
  const vc=v=>rows.filter(r=>r.evaluation?.verdict===v).length;
  const buy=vc('買い'), watch=vc('様子見'), pass=vc('見送り'), nev=n-ev.length;

  // --- 計算をすべて先に集約（表示順に依存させない）---
  // 能力適合
  const fits=ev.map(r=>r.evaluation.capability_fit).filter(v=>v!=null);
  const reach=fits.filter(v=>v>=4).length, fd=k=>fits.filter(v=>v===k).length;
  // YouTube照合
  const yt=rows.filter(r=>r.candidates&&r.candidates.length);
  const confs=yt.map(r=>r.candidates[0].confidence).filter(v=>v!=null);
  const hi=confs.filter(v=>v>=0.8).length;
  const bench=rows.filter(r=>r.candidates&&r.candidates.some(c=>c.benchmark)).length;
  const cmed=_med(confs);
  // 市場乖離
  const tgt=rows.filter(r=>(r.flags||[]).some(f=>f.startsWith('交渉'))).length;
  const undervalued=rows.filter(r=>r.status_state==='受付終了'&&r.evaluation&&r.evaluation.overall_score>=2).length;
  // 価格・利益
  const pmed=_med(rows.map(r=>r.price).filter(v=>v!=null));
  const profmed=_med(rows.map(r=>r.profit).filter(v=>v!=null));
  const pbmed=_med(rows.map(r=>r.metrics?.payback_months_recent).filter(v=>v!=null));
  // 市場の速さ・即決ゾーン
  const sr=rows.filter(r=>r.status_state==='成約済み'&&r.dwell_days!=null);
  let spdT='成約済みなし', spdB='<div class="ov-note">状態=成約済み/すべて で表示されます。</div>';
  if(sr.length){
    const dw=sr.map(r=>r.dwell_days);
    const fast=sr.filter(r=>r.dwell_days<=7), w30=sr.filter(r=>r.dwell_days<=30), w90=sr.filter(r=>r.dwell_days<=90), slow=sr.filter(r=>r.dwell_days>30);
    const fp=_med(fast.map(r=>r.price).filter(v=>v!=null)), sp=_med(slow.map(r=>r.price).filter(v=>v!=null));
    const fpr=_med(fast.map(r=>r.profit).filter(v=>v!=null)), spr=_med(slow.map(r=>r.profit).filter(v=>v!=null));
    const toks=_toks(fast);
    spdT=`滞留中央 ${_med(dw)}日 · 即決${_pct(fast.length,sr.length)}%`;
    spdB=_chart([{t:'≤7日',v:fast.length,c:_RAMP4[0]},{t:'8-30',v:w30.length-fast.length,c:_RAMP4[1]},{t:'31-90',v:w90.length-w30.length,c:_RAMP4[2]},{t:'>90',v:sr.length-w90.length,c:_RAMP4[3]}])
     +_ovRow('滞留 中央値',_med(dw)+'日',true)
     +_ovRow('即決 ≤7日',`<span class="ov-hi">${fast.length}件 (${_pct(fast.length,sr.length)}%)</span>`)
     +_ovRow('≤30日 / ≤90日',`${_pct(w30.length,sr.length)}% / ${_pct(w90.length,sr.length)}%`)
     +_ovRow('価格 即決↔じっくり',`${yen(fp)} <span class="k">(n${fast.length})</span> ↔ ${yen(sp)} <span class="k">(n${slow.length})</span>`)
     +_ovRow('利益 即決↔じっくり',`${yen(fpr)} <span class="k">(n${fast.length})</span> ↔ ${yen(spr)} <span class="k">(n${slow.length})</span>`)
     +(toks.length?`<div class="ov-note">即決(≤7日)の頻出ジャンル: ${_tags(toks,6)}</div>`:'');
  }
  // 判定×成約
  const se=sr.filter(r=>r.evaluation);
  let mT='成約データなし', mB='<div class="ov-note">状態=成約済み/すべて で表示されます。</div>';
  if(se.length){
    const sb=se.filter(r=>r.evaluation.verdict==='買い').length, sw=se.filter(r=>r.evaluation.verdict==='様子見').length, sp2=se.filter(r=>r.evaluation.verdict==='見送り').length;
    mT=`買い${_pct(sb,se.length)}%（成約${se.length}件）`;
    mB=_chart([{t:'買い',v:sb,c:_CBUY},{t:'様子見',v:sw,c:_CWATCH},{t:'見送り',v:sp2,c:_CPASS}])
     +_ovRow('買い',`<span class="ov-hi">${sb}件 (${_pct(sb,se.length)}%)</span>`)
     +_ovRow('様子見',`${sw}件 (${_pct(sw,se.length)}%)`)
     +_ovRow('見送り',`<span class="ov-lo">${sp2}件 (${_pct(sp2,se.length)}%)</span>`)
     +`<div class="ov-note">市場が買った案件を我々はどう判定していたか（総合≠市場の検証）。</div>`;
  }
  // リスク/実績フラグ分布
  const ft={}; rows.forEach(r=>(r.flags||[]).forEach(f=>{ ft[f]=(ft[f]||0)+1; }));
  const fl=Object.entries(ft).sort((a,b)=>b[1]-a[1]);
  // データ品質
  const series=rows.filter(r=>r.metrics&&r.metrics.monetized_months!=null).length;
  const ages=rows.map(r=>r.data_age_months).filter(v=>v!=null);
  // 制作様式
  const at=_toks(rows);

  // --- 表示順（指定）---
  let H='';
  // 1 構成
  H+=_ovSec('compose','構成',`${n}件 · 募${open}/成${sold}/終${ended}`,
    _chart([{t:'募集',v:open,c:_COPEN},{t:'成約',v:sold,c:_CSOLD},{t:'終了',v:ended,c:_CEND}])
   +_chart([{t:'買い',v:buy,c:_CBUY},{t:'様子見',v:watch,c:_CWATCH},{t:'見送り',v:pass,c:_CPASS},{t:'未評価',v:nev,c:_CNONE}])
   +_ovRow('状態',`募集 ${open} / 成約 ${sold} / 受付終了 ${ended}`)
   +_ovRow('判定',`<span class="ov-hi">買 ${buy}</span> · 様 ${watch} · 見 ${pass} · <span class="mut">未 ${nev}</span>`));
  // 2 能力適合
  H+=_ovSec('fit','能力適合（作れる射程）',
    fits.length?`射程≥4: <span class="ov-hi">${reach}</span> (${_pct(reach,fits.length)}%)`:'評価なし',
    fits.length?(_chart([{t:'適合5',v:fd(5),c:_RAMP5[0]},{t:'適合4',v:fd(4),c:_RAMP5[1]},{t:'適合3',v:fd(3),c:_RAMP5[2]},{t:'適合2',v:fd(2),c:_RAMP5[3]},{t:'適合1',v:fd(1),c:_RAMP5[4]}])
     +_ovRow('射程 ≥4（作れる）',`<span class="ov-hi">${reach}件 / 評価${fits.length}件 (${_pct(reach,fits.length)}%)</span>`,true)
     +_ovRow('適合 5 / 4',`${fd(5)} / ${fd(4)}`)
     +_ovRow('適合 3 / 2 / 1',`${fd(3)} / ${fd(2)} / ${fd(1)}`)):'<div class="ov-note">評価済みがありません。</div>');
  // 3 市場の速さ・即決ゾーン
  H+=_ovSec('speed','市場の速さ・即決ゾーン',spdT,spdB);
  // 4 判定×成約
  H+=_ovSec('match','判定 × 成約',mT,mB);
  // 5 YouTube照合カバレッジ
  H+=_ovSec('yt','YouTube照合カバレッジ',
    `照合 ${yt.length}/${n} (${_pct(yt.length,n)}%)${cmed!=null?` · 一致中央 ${Math.round(cmed*100)}%`:''}`,
    _chart([{t:'照合済',v:yt.length,c:'#6db3f2'},{t:'未照合',v:n-yt.length,c:_CDARK}],true)
   +_ovRow('照合済み',`<span class="ov-hi">${yt.length}件</span> / ${n}件 (${_pct(yt.length,n)}%)`,true)
   +_ovRow('一致率 中央値',cmed==null?'–':Math.round(cmed*100)+'%')
   +_ovRow('高一致 ≥80%',`${hi}件`)
   +_ovRow('ベンチ取得済み',`${bench}件`));
  // 6 データ品質・カバレッジ
  H+=_ovSec('quality','データ品質・カバレッジ',`評価 ${_pct(ev.length,n)}% · 系列 ${_pct(series,n)}%`,
    _chart([{t:'評価済',v:ev.length,c:_CSOLD},{t:'未評価',v:n-ev.length,c:_CDARK}],true)
   +_chart([{t:'系列あり',v:series,c:'#6db3f2'},{t:'系列なし',v:n-series,c:_CDARK}],true)
   +_ovRow('評価済み',`${ev.length}件 / ${n}件 (${_pct(ev.length,n)}%)`,true)
   +_ovRow('収益系列あり',`${series}件 (${_pct(series,n)}%)`)
   +_ovRow('データ鮮度 中央値',ages.length?_med(ages)+'ヶ月前':'–')
   +`<div class="ov-note">カバレッジ偏り・系列欠落・鮮度を把握し集計を過信しないため。</div>`);
  // 7 市場乖離・機会
  H+=_ovSec('gap','市場乖離・機会',`🎯${tgt} · 割安候補${undervalued}`,
    _ovRow('🎯 交渉ターゲット（活性）',`<span class="ov-hi">${tgt}件</span>`,true)
   +_ovRow('高評価×売れ残り（受付終了×総合≥2）',`${undervalued}件`)
   +`<div class="ov-note">高評価なのに売れず取り下げ＝強気価格の割安交渉余地（本家を超える核心）。</div>`);
  // 8 価格・利益
  H+=_ovSec('money','価格・利益（中央値）',`価格 ${yen(pmed)}`,
    _ovRow('価格 中央値',yen(pmed),true)
   +_ovRow('利益/月 中央値',yen(profmed))
   +_ovRow('回収 中央値',pbmed==null?'–':pbmed+'ヶ月'));
  // 9 リスク/実績フラグ分布
  H+=_ovSec('flags','リスク/実績フラグ分布',
    fl.length?`${esc(fl[0][0])} ${fl[0][1]}${fl.length>1?` 他${fl.length-1}種`:''}`:'フラグなし',
    fl.length?_tags(fl,14):'<div class="ov-note">フラグはありません。</div>');
  // 10 制作様式（頻出ジャンル語）
  H+=_ovSec('style','制作様式（頻出ジャンル語）',
    at.length?`${esc(at[0][0])} ${at[0][1]} 他`:'語なし',
    at.length?_tags(at,16)+`<div class="ov-note">勝ち筋はトピックでなく制作様式（AI/顔なし/まとめ…）に出る。</div>`:'<div class="ov-note">評価済みジャンルがありません。</div>');

  el.innerHTML=`<div class="ov-grid2">${H}</div>`;
}

// ===== ★ブックマーク（localStorage・サーバ不要・再生成耐性あり）=====
let _bm = new Set([...(()=>{ try{ return JSON.parse(localStorage.getItem('rakkoma_bookmarks')||'[]'); }catch(e){ return []; } })(), ...(_dbBookmarks||[])].map(String));
function isBm(id){ return _bm.has(String(id)); }
function bmCount(){ const el=document.getElementById('bmcnt'); if(el) el.textContent=_bm.size?`(${_bm.size})`:''; }
function bmToggle(id, ev){ ev.stopPropagation(); id=String(id);
  if(_bm.has(id)) _bm.delete(id); else _bm.add(id);
  try{ localStorage.setItem('rakkoma_bookmarks', JSON.stringify([..._bm])); }catch(e){}
  bmCount();
  if(document.getElementById('bmOnly').checked){ render(); }   // ★のみ表示中は外したら消す
  else { const el=ev.currentTarget, on=_bm.has(id); el.textContent=on?'★':'☆'; el.classList.toggle('on',on); }
}
function bmExport(){
  const ids=[..._bm];
  const blob=new Blob([JSON.stringify(ids)], {type:'application/json'});
  const url=URL.createObjectURL(blob);
  const a=document.createElement('a'); a.href=url; a.download='bookmarks.json'; document.body.appendChild(a); a.click();
  a.remove(); URL.revokeObjectURL(url);
}

render();
bmCount();
setCtrlH();
window.addEventListener('resize', setCtrlH);
</script>
</body>
</html>
"""


def main() -> None:
    conn = DB.init()
    rows = DB.fetch_dashboard_rows(conn)
    cands, bench = _load_youtube(conn)
    for r in rows:
        cs = cands.get(str(r["id"]))   # listings.id=int / channel_candidates.listing_id=text のため正規化
        if cs:
            for c in cs:
                b = bench.get(c["channel_id"])
                # ベンチ/レポートは「生成元の案件」でのみ紐付ける（別案件に同chが候補で出ても漏らさない）
                if b and b.get("listing_id") == str(r["id"]):
                    c["benchmark"] = b
            r["candidates"] = cs
        ev = r.get("evaluation")
        if ev and ev.get("genre"):
            # 括弧の補足を除去 → 短縮辞書。フルは tooltip/検索用に保持し、主ジャンル1語(先頭トークン)を別途持つ
            g = re.sub(r"[（(][^）)]*[）)]", "", ev["genre"]).strip(" /　/")
            g = abbr_genre(g)
            ev["genre"] = g
            ev["genre_main"] = re.split(r"[／/・、,\s]+", g)[0] if g else ""
        # 運営乖離（収益化月数 ≫ 運営月数 = 再販/移管の疑い）をフラグに格上げ
        if (r.get("history_gap") or 0) >= 3 and "運営乖離" not in r.get("flags", []):
            r.setdefault("flags", []).append("運営乖離")
        # 交渉ターゲット: 募集中×(買い/様子見)×総合≥2.0×持続≥3×滞留≥30 = 収益健全なのに高くて売れ残り
        ov = (ev or {}).get("overall_score")
        sus = ((ev or {}).get("scores") or {}).get("sustainability") or 0
        if (r.get("status_state") == "募集中" and ov is not None and ov >= 2.0
                and (ev or {}).get("verdict") in ("買い", "様子見") and sus >= 3
                and (r.get("days_listed") or 0) >= 30):
            r.setdefault("flags", []).append("交渉ターゲット")
    data = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
    try:                                              # DB の bookmarks テーブル（あれば）を★の種にする
        bms = [str(r[0]) for r in conn.execute("SELECT listing_id FROM bookmarks")]
    except sqlite3.OperationalError:
        bms = []
    generated = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    html = (HTML.replace("__DATA__", data)
                .replace("__BOOKMARKS__", json.dumps(bms))
                .replace("__GENERATED__", generated))
    DASHBOARD_FILE.write_text(html, encoding="utf-8")
    n_eval = sum(1 for r in rows if r.get("evaluation"))
    print(f"生成: {DASHBOARD_FILE} （全{len(rows)}件 / 評価済み{n_eval}件）")


if __name__ == "__main__":
    main()
