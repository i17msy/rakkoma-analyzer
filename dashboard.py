#!/usr/bin/env python3
"""data/listings/*.json から静的ダッシュボード data/dashboard.html を生成。

LLM評価（再現性重視）+ 定量メトリクスを一覧化。総合スコア順ソート・列ヘッダで
並べ替え・行クリックで強み/弱み/再現メモを展開。サーバー不要、ブラウザで開くだけ。

  python3 dashboard.py
"""

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import DASHBOARD_FILE
import storage as DB

JST = timezone(timedelta(hours=9))


def load() -> list[dict]:
    return DB.fetch_dashboard_rows(DB.init())


HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ラッコM&A アナライザー</title>
<style>
  :root { --bg:#0f1419; --panel:#1a2029; --line:#2b3543; --fg:#e6edf3; --mut:#8b98a9;
          --good:#3fb950; --mid:#d29922; --bad:#f85149; --accent:#58a6ff; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font-family:-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif; font-size:13px; }
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
       top:0; background:var(--bg); font-size:11.5px; }
  th:hover { color:var(--fg); }
  th.num, td.num { text-align:right; font-variant-numeric:tabular-nums; }
  tr.row { cursor:pointer; }
  tr.row:hover > td { background:#161d27; }
  td.title { white-space:normal; max-width:380px; }
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
  .detail td { background:#11161e; padding:0; }
  .detail .inner { padding:14px 18px 18px; display:grid; grid-template-columns:1fr 1fr; gap:18px; }
  .detail h4 { margin:0 0 6px; font-size:12px; color:var(--mut); text-transform:uppercase;
               letter-spacing:.04em; }
  .detail ul { margin:0; padding-left:18px; } .detail li { margin:3px 0; line-height:1.5; }
  .detail .full { grid-column:1 / -1; }
  .detail .note { background:var(--panel); border:1px solid var(--line); border-radius:8px;
                  padding:10px 12px; line-height:1.6; }
  .bars { display:flex; gap:14px; flex-wrap:wrap; }
  .bar { font-size:12px; } .bar b { color:var(--fg); }
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
  <input id="q" placeholder="タイトル・ジャンル検索…" oninput="render()">
  <select id="sf" onchange="render()">
    <option value="">状態: すべて</option>
    <option value="募集中">募集中</option>
    <option value="成約済み">成約済み</option>
    <option value="受付終了">受付終了</option>
  </select>
  <select id="vf" onchange="render()">
    <option value="">判定: すべて</option>
    <option value="買い">買い</option>
    <option value="様子見">様子見</option>
    <option value="見送り">見送り</option>
    <option value="__none">未評価</option>
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
  {k:'state',  label:'状態',     get:r=>r.status_state ?? null, statePill:true},
  {k:'verdict',label:'判定',     get:r=>r.evaluation?.verdict ?? null},
  {k:'overall',label:'総合',     get:r=>r.evaluation?.overall_score ?? null, num:true, score:true},
  {k:'profit', label:'月利益',   get:r=>r.metrics?.profit_recent ?? r.profit ?? null, num:true, money:true},
  {k:'price',  label:'価格',     get:r=>r.price ?? null, num:true, money:true},
  {k:'payback',label:'回収月',   get:r=>r.metrics?.payback_months_recent ?? null, num:true},
  {k:'deal',   label:'成約日',   get:r=>r.deal_days ?? null, num:true},
  {k:'rep',    label:'再現',     get:r=>r.evaluation?.scores?.replicability ?? null, num:true, s5:true},
  {k:'sus',    label:'持続',     get:r=>r.evaluation?.scores?.sustainability ?? null, num:true, s5:true},
  {k:'val',    label:'割安',     get:r=>r.evaluation?.scores?.value ?? null, num:true, s5:true},
  {k:'gro',    label:'成長',     get:r=>r.evaluation?.scores?.growth ?? null, num:true, s5:true},
  {k:'stab',   label:'安定度',   get:r=>r.metrics?.stability ?? null, num:true},
  {k:'genre',  label:'ジャンル', get:r=>r.evaluation?.genre||'', cls:'genre'},
];
let sortKey='overall', sortDir=-1;

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
function cell(c,r){
  let v=c.get(r);
  if(c.cls==='title'){
    const u=r.url||'#';
    return `<a href="${u}" target="_blank" rel="noopener">${esc(v)}</a>`;
  }
  if(c.statePill) return stPill(v);
  if(c.k==='verdict') return vPill(v);
  if(v==null) return '<span class="mut">–</span>';
  if(c.money) return yen(v);
  if(c.score) return `<span class="score ${sCls(v,5)}">${v}</span>`;
  if(c.s5)    return `<span class="${sCls(v,5)}">${v}</span>`;
  if(c.k==='payback') return v+'ヶ月';
  if(c.k==='deal')    return v+'日';
  return esc(String(v));
}
function esc(s){ return (s||'').replace(/[&<>"]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m])); }

function detailRow(r,span){
  const e=r.evaluation, m=r.metrics||{};
  if(!e){
    return `<tr class="detail hidden"><td colspan="${span}"><div class="inner">
      <div class="full mut">未評価。<code>python3 analyze.py --id ${r.id}</code> で評価できます。</div>
      </div></td></tr>`;
  }
  const li=a=>(a||[]).map(x=>`<li>${esc(x)}</li>`).join('');
  return `<tr class="detail hidden"><td colspan="${span}"><div class="inner">
    <div><h4>強み</h4><ul>${li(e.strengths)}</ul></div>
    <div><h4>弱み・リスク</h4><ul>${li(e.weaknesses)}</ul></div>
    <div class="full"><h4>再現メモ（自分で作るなら）</h4><div class="note">${esc(e.replication_note)}</div></div>
    <div class="full"><h4>判定理由</h4><div class="note">${vPill(e.verdict)} ${esc(e.verdict_reason)}</div></div>
    <div class="full bars">
      <span class="bar mut">登録者 <b>${esc(r.followers_str||'–')}</b></span>
      <span class="bar mut">平均/最高利益 <b>${yen(m.profit_avg)} / ${yen(m.profit_max)}</b></span>
      <span class="bar mut">登録者1k人あたり利益 <b>${yen(m.profit_per_1k_subs)}</b></span>
      <span class="bar mut">収益モデル <b>${esc(r.biz_model||'–')}</b></span>
    </div>
  </div></td></tr>`;
}

function render(){
  const q=document.getElementById('q').value.trim().toLowerCase();
  const vf=document.getElementById('vf').value;
  const sf=document.getElementById('sf').value;
  const evalOnly=document.getElementById('evalOnly').checked;

  let rows=DATA.filter(r=>{
    if(evalOnly && !r.evaluation) return false;
    if(sf && r.status_state!==sf) return false;
    if(vf==='__none' && r.evaluation) return false;
    if(vf && vf!=='__none' && r.evaluation?.verdict!==vf) return false;
    if(q){ const hay=((r.title||'')+' '+(r.evaluation?.genre||'')).toLowerCase();
           if(!hay.includes(q)) return false; }
    return true;
  });

  const col=COLS.find(c=>c.k===sortKey);
  rows.sort((a,b)=>{
    let x=col.get(a), y=col.get(b);
    x=(x==null)?-Infinity:(typeof x==='number'?x:String(x));
    y=(y==null)?-Infinity:(typeof y==='number'?y:String(y));
    if(x<y) return -sortDir; if(x>y) return sortDir; return 0;
  });

  document.getElementById('cnt').textContent=DATA.length;
  document.getElementById('head').innerHTML=COLS.map(c=>{
    const ar=sortKey===c.k?(sortDir<0?' ▾':' ▴'):'';
    return `<th class="${c.num?'num':''}" onclick="sortBy('${c.k}')">${c.label}${ar}</th>`;
  }).join('');

  const span=COLS.length;
  document.getElementById('body').innerHTML=rows.map((r,i)=>{
    const tds=COLS.map(c=>`<td class="${c.num?'num ':''}${c.cls||''}">${cell(c,r)}</td>`).join('');
    return `<tr class="row" onclick="toggle(this)">${tds}</tr>`+detailRow(r,span);
  }).join('');
}
function sortBy(k){ if(sortKey===k) sortDir*=-1; else { sortKey=k; sortDir=-1; } render(); }
function toggle(tr){ tr.nextElementSibling.classList.toggle('hidden'); }
render();
</script>
</body>
</html>
"""


def main() -> None:
    rows = load()
    data = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
    generated = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    html = HTML.replace("__DATA__", data).replace("__GENERATED__", generated)
    DASHBOARD_FILE.write_text(html, encoding="utf-8")
    n_eval = sum(1 for r in rows if r.get("evaluation"))
    print(f"生成: {DASHBOARD_FILE} （全{len(rows)}件 / 評価済み{n_eval}件）")


if __name__ == "__main__":
    main()
