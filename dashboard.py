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
            "SELECT listing_id, channel_id, channel_title, subs, videos, published, confidence, thumbs_json "
            "FROM channel_candidates WHERE status='candidate' ORDER BY confidence DESC"):
            cands.setdefault(r[0], []).append({
                "channel_id": r[1], "title": r[2], "subs": r[3],
                "videos": r[4], "published": r[5], "confidence": r[6],
                "thumbs": json.loads(r[7]) if r[7] else []})
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
              border-bottom:1px solid var(--line); }
  .controls input, .controls select { background:var(--panel); color:var(--fg);
              border:1px solid var(--line); border-radius:6px; padding:6px 9px; font-size:12px; }
  .controls .sp { flex:1; }
  table { width:100%; border-collapse:collapse; }
  th, td { padding:9px 10px; text-align:left; border-bottom:1px solid var(--line);
           white-space:nowrap; }
  th { color:var(--mut); font-weight:600; cursor:pointer; user-select:none; position:sticky;
       top:0; background:var(--bg); font-size:12.5px; }
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
  .bars.scores { gap:22px; padding-bottom:6px; border-bottom:1px solid var(--line); }
  .scores .bar { font-size:18px; } .scores b { font-size:23px; font-weight:700; }
  .scores b.s-hi { color:var(--good); } .scores b.s-mid { color:var(--mid); } .scores b.s-lo { color:var(--bad); }
  .bar b.s-lo { color:var(--bad); } .bar b.s-mid { color:var(--mid); } .bar b.s-hi { color:var(--good); }
  .freshbar { padding:5px 10px; border-radius:6px; background:#1a2029; display:inline-block; font-size:14px; }
  .detail h4.hStr { color:var(--good); } .detail h4.hWeak { color:var(--mid); }
  .ytsec { background:#0a1119; border:1px solid #25405a; border-left:3px solid #c4302b;
           border-radius:8px; padding:11px 14px 13px; margin-top:6px; }
  .ytsec h4 { color:#ff7a6b; margin:0 0 8px; font-size:17px; }
  .ytc { padding:9px 2px; border-top:1px solid #1b2937; line-height:1.65; font-size:15px; }
  .ytc:first-of-type { border-top:none; }
  .ytcline { font-size:16.5px; }
  .ytcline > b { display:inline-block; min-width:48px; }
  .ytcline a { color:#6db3f2; margin:0 9px; text-decoration:none; } .ytcline a:hover { text-decoration:underline; }
  .ytstrip { display:flex; flex-wrap:wrap; gap:5px; margin:8px 0 2px; }
  .ytth { height:80px; width:auto; border-radius:4px; border:1px solid #1c2a3a; display:block; }
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
</style>
</head>
<body>
<header>
  <h1>🦦 ラッコM&A アナライザー</h1>
  <span class="meta">再現性重視評価 · <span id="cnt"></span>件 · 生成 __GENERATED__</span>
</header>
<div class="controls">
  <input id="q" placeholder="ID・タイトル・ジャンル検索…" oninput="render()">
  <select id="sf" onchange="render()">
    <option value="">状態: すべて</option>
    <option value="募集中">募集中</option>
    <option value="__closed">クローズ</option>
    <option value="成約済み">成約済み</option>
    <option value="受付終了">受付終了</option>
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
  <span class="sp"></span>
  <span class="meta mut">行クリックで詳細展開</span>
</div>
<table>
  <thead><tr id="head"></tr></thead>
  <tbody id="body"></tbody>
</table>

<script>
const DATA = __DATA__;
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
  {k:'stale',  label:'滞留',     get:r=>r.days_listed ?? null, num:true},
  {k:'listed', label:'掲載',     get:r=>r.listed_at ?? null, cls:'date', align:'right'},
  {k:'genre',  label:'ジャンル', get:r=>r.evaluation?.genre||'', cls:'genre'},
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
  if(c.k==='flags'){
    const m=(r.candidates&&r.candidates.length)?` <span class="ytdone" title="YouTube候補出し済み（${r.candidates.length}件）">🎥</span>`:'';
    return flagCell(v)+m;
  }
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
  if(c.k==='deal' || c.k==='stale') return v+'日';
  return esc(String(v));
}
function esc(s){ return (s||'').replace(/[&<>"]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m])); }

function ytConf(v){ return v>=0.7?'s-hi':v>=0.5?'s-mid':'s-lo'; }
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
    return `<div class="ytc"><div class="ytcline">`
      +`<b class="${ytConf(c.confidence)}">${Math.round(c.confidence*100)}%</b>`
      +`<a href="https://www.youtube.com/channel/${c.channel_id}" target="_blank" rel="noopener">${esc(c.title)}</a>`
      +`<span class="mut">登録${subs} / 投稿${c.videos} / 開設${c.published||'-'}</span></div>`
      +(thumbs?`<div class="ytstrip">${thumbs}</div>`:'')+`</div>`;
  }).join('');
  return `<div class="full ytsec"><h4>🎥 YouTube候補（近似順・サムネで設計を見る）</h4>${items}</div>`;
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
    <div class="full"><a href="${r.url||'#'}" target="_blank" rel="noopener" class="dtitle">${esc(r.title||'')}</a></div>
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
  const evalOnly=document.getElementById('evalOnly').checked;

  let rows=DATA.filter(r=>{
    if(evalOnly && !r.evaluation) return false;
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
  rows.sort((a,b)=>{
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
  document.getElementById('head').innerHTML=COLS.map(c=>{
    const ar=sortKey===c.k?(sortDir<0?' ▾':' ▴'):'';
    return `<th class="${acl(c)}" onclick="sortBy('${c.k}')">${c.label}${ar}</th>`;
  }).join('');

  const span=COLS.length;
  document.getElementById('body').innerHTML=rows.map((r,i)=>{
    const tds=COLS.map(c=>`<td class="${acl(c)} ${c.cls||''}">${cell(c,r)}</td>`).join('');
    return `<tr class="row" onclick="toggle(this)">${tds}</tr>`+detailRow(r,span);
  }).join('');
}
function sortBy(k){ if(sortKey===k) sortDir*=-1; else { sortKey=k; sortDir=(k==='verdict')?1:-1; } render(); }
function toggle(tr){ const d=tr.nextElementSibling; const opening=d.classList.contains('hidden'); d.classList.toggle('hidden'); tr.classList.toggle('open', opening); }
function closeDetail(ev,btn){ ev.stopPropagation(); const d=btn.closest('tr.detail'); d.classList.add('hidden'); if(d.previousElementSibling) d.previousElementSibling.classList.remove('open'); }
render();
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
            # 括弧の補足を除去 → 短縮辞書 → 念のため20字で丸め
            g = re.sub(r"[（(][^）)]*[）)]", "", ev["genre"]).strip(" /　/")
            g = abbr_genre(g)
            ev["genre"] = g if len(g) <= 20 else g[:19] + "…"
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
    generated = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    html = HTML.replace("__DATA__", data).replace("__GENERATED__", generated)
    DASHBOARD_FILE.write_text(html, encoding="utf-8")
    n_eval = sum(1 for r in rows if r.get("evaluation"))
    print(f"生成: {DASHBOARD_FILE} （全{len(rows)}件 / 評価済み{n_eval}件）")


if __name__ == "__main__":
    main()
