"""SQLite ストレージ層（アナライザーの分析・ダッシュボードの土台）。

テーブル:
  listings       … 案件の最新スナップショット（メトリクス列を含む）
  price_history  … 価格・ステータスの変化履歴（値下げ追跡・成約タイミング）
  evaluations    … LLM評価の最新結果（listing_id ごと1行、REPLACEで更新）

raw_json に詳細の生データを保持し、よく検索する列だけ正規化して持つハイブリッド。
"""

import json
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from config import DATA_DIR

JST = timezone(timedelta(hours=9))
DB_FILE = DATA_DIR / "rakkoma.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id            INTEGER PRIMARY KEY,
    url           TEXT,
    title         TEXT,
    category      TEXT,
    biz_model     TEXT,
    content_type  TEXT,
    asset_type    TEXT,          -- 譲渡物種別（公式分類: アカウント（YouTube）/ECサイト/WEBメディア 等）
    status        TEXT,          -- 詳細ページの「現在の運営状況」
    status_state  TEXT,          -- 募集中 / 成約済み / 受付終了
    deal_days     INTEGER,       -- 成約までの日数（成約済みのみ）
    listed_at     TEXT,          -- 公開日（ラッコ掲載日）
    updated_at    TEXT,          -- 出品情報の更新日
    price         INTEGER,
    price_str     TEXT,
    ratio_str     TEXT,
    profit        INTEGER,
    profit_str    TEXT,
    revenue       INTEGER,
    revenue_str   TEXT,
    followers     INTEGER,
    followers_str TEXT,
    post_count    TEXT,
    start_date    TEXT,
    description   TEXT,
    -- 定量メトリクス
    profit_recent         INTEGER,
    profit_avg            INTEGER,
    profit_max            INTEGER,
    profit_min            INTEGER,
    months                INTEGER,
    monetized_months      INTEGER,
    leading_zeros         INTEGER,
    recent_vs_max         REAL,
    payback_months_recent REAL,
    payback_months_avg    REAL,
    stability             REAL,
    cv                    REAL,
    trend                 REAL,
    flags                 TEXT,
    profit_series         TEXT,
    profit_per_1k_subs    INTEGER,
    -- 管理
    raw_json     TEXT,
    first_seen   TEXT,
    last_seen    TEXT,
    fetched_at   TEXT
);
CREATE TABLE IF NOT EXISTS price_history (
    listing_id   INTEGER,
    price        INTEGER,
    status_state TEXT,
    seen_at      TEXT
);
CREATE TABLE IF NOT EXISTS evaluations (
    listing_id      INTEGER PRIMARY KEY,
    overall_score   REAL,
    replicability   INTEGER,
    sustainability  INTEGER,
    value           INTEGER,
    growth          INTEGER,
    capability_fit  INTEGER,
    risk_factor     REAL,
    genre           TEXT,
    verdict         TEXT,
    verdict_reason  TEXT,
    summary         TEXT,
    strengths_json  TEXT,
    weaknesses_json TEXT,
    replication_note TEXT,
    model           TEXT,
    evaluated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_listings_state ON listings(status_state);
CREATE INDEX IF NOT EXISTS idx_eval_overall   ON evaluations(overall_score);
"""

_METRIC_COLS = ["profit_recent", "profit_avg", "profit_max", "profit_min", "months",
                "monetized_months", "leading_zeros", "recent_vs_max",
                "payback_months_recent", "payback_months_avg", "stability", "cv", "trend",
                "profit_per_1k_subs"]
_LISTING_COLS = ["url", "title", "category", "biz_model", "content_type", "status",
                 "status_state", "deal_days", "listed_at", "updated_at",
                 "price", "price_str", "ratio_str",
                 "profit", "profit_str", "revenue", "revenue_str", "followers",
                 "followers_str", "post_count", "start_date", "description", "asset_type"]


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init(conn: sqlite3.Connection | None = None) -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = conn or connect()
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """既存DBへの後方互換マイグレーション（カラム追加）。"""
    def cols(tbl):
        return {r[1] for r in conn.execute(f"PRAGMA table_info({tbl})")}
    ec = cols("evaluations")
    if "capability_fit" not in ec:
        conn.execute("ALTER TABLE evaluations ADD COLUMN capability_fit INTEGER")
    if "risk_factor" not in ec:
        conn.execute("ALTER TABLE evaluations ADD COLUMN risk_factor REAL")
    lc = cols("listings")
    for c in ("listed_at", "updated_at", "flags", "profit_series", "asset_type"):
        if c not in lc:
            conn.execute(f"ALTER TABLE listings ADD COLUMN {c} TEXT")
    for c in ("profit_min", "months", "monetized_months", "leading_zeros"):
        if c not in lc:
            conn.execute(f"ALTER TABLE listings ADD COLUMN {c} INTEGER")
    for c in ("recent_vs_max", "cv", "trend"):
        if c not in lc:
            conn.execute(f"ALTER TABLE listings ADD COLUMN {c} REAL")


def upsert_listing(conn: sqlite3.Connection, detail: dict, metrics: dict, title: str = "") -> None:
    """案件をupsert。価格 or ステータスが前回と変われば price_history に記録。"""
    now = datetime.now(JST).isoformat()
    pid = int(detail["id"])
    title = title or detail.get("title", "")

    prev = conn.execute("SELECT price, status_state, first_seen FROM listings WHERE id=?", (pid,)).fetchone()
    first_seen = prev["first_seen"] if prev else now

    row = {c: detail.get(c) for c in _LISTING_COLS}
    row.update({c: metrics.get(c) for c in _METRIC_COLS})
    row.update({"id": pid, "title": title, "raw_json": json.dumps(detail, ensure_ascii=False),
                "flags": json.dumps(metrics.get("flags", []), ensure_ascii=False),
                "profit_series": json.dumps(detail.get("profit_series", []), ensure_ascii=False),
                "first_seen": first_seen, "last_seen": now, "fetched_at": detail.get("fetched_at", now)})

    cols = list(row.keys())
    conn.execute(
        f"INSERT OR REPLACE INTO listings ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
        [row[c] for c in cols],
    )
    # 価格 or ステータス変化を履歴に
    if prev is None or prev["price"] != detail.get("price") or prev["status_state"] != detail.get("status_state"):
        conn.execute("INSERT INTO price_history (listing_id, price, status_state, seen_at) VALUES (?,?,?,?)",
                     (pid, detail.get("price"), detail.get("status_state"), now))
    conn.commit()


def save_evaluation(conn: sqlite3.Connection, listing_id: int, ev: dict) -> None:
    s = ev.get("scores", {})
    conn.execute(
        """INSERT OR REPLACE INTO evaluations
           (listing_id, overall_score, replicability, sustainability, value, growth, capability_fit,
            risk_factor, genre, verdict, verdict_reason, summary, strengths_json, weaknesses_json,
            replication_note, model, evaluated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (int(listing_id), ev.get("overall_score"), s.get("replicability"), s.get("sustainability"),
         s.get("value"), s.get("growth"), ev.get("capability_fit"), ev.get("risk_factor"),
         ev.get("genre"), ev.get("verdict"), ev.get("verdict_reason"),
         ev.get("summary"), json.dumps(ev.get("strengths", []), ensure_ascii=False),
         json.dumps(ev.get("weaknesses", []), ensure_ascii=False), ev.get("replication_note"),
         ev.get("model"), ev.get("evaluated_at")),
    )
    conn.commit()


def _load_list(s: str | None) -> list:
    """strengths/weaknesses を必ずリストで返す（LLMが文字列で返した不正も吸収）。"""
    try:
        v = json.loads(s or "[]")
    except Exception:
        return [s] if s else []
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        t = v.strip()
        if t.startswith("["):
            try:
                p = json.loads(t)
                if isinstance(p, list):
                    return p
            except Exception:
                pass
        return [t] if t else []
    return []


def fetch_dashboard_rows(conn: sqlite3.Connection) -> list[dict]:
    """ダッシュボードJS が期待するネスト構造に整形して返す。"""
    rows = conn.execute("""
        SELECT l.*, e.overall_score, e.replicability, e.sustainability, e.value AS ev_value,
               e.growth, e.capability_fit, e.risk_factor, e.genre, e.verdict, e.verdict_reason, e.summary,
               e.strengths_json, e.weaknesses_json, e.replication_note, e.model, e.evaluated_at
        FROM listings l LEFT JOIN evaluations e ON e.listing_id = l.id
        ORDER BY e.overall_score DESC NULLS LAST, l.profit_recent DESC NULLS LAST
    """).fetchall()
    from datetime import date
    today = datetime.now(JST).date()

    def _days_since(s):
        try:
            y, m, dd = map(int, s.split("-"))
            return (today - date(y, m, dd)).days
        except Exception:
            return None

    def _months_since(s):
        """YYYY-MM-DD → 今日までの月数（データ鮮度用）。"""
        if not s:
            return None
        m = re.match(r"(\d{4})-(\d{1,2})", s)
        return max(0, (today.year - int(m.group(1))) * 12 + (today.month - int(m.group(2)))) if m else None

    def _operating_months(s):
        """運営開始時期 'YYYY年MM月' → 今日までの運営月数。"""
        if not s:
            return None
        m = re.search(r"(\d{4})年(\d{1,2})月", s) or re.search(r"(\d{4})年", s)
        if not m:
            return None
        y = int(m.group(1))
        mo = int(m.group(2)) if m.lastindex and m.lastindex >= 2 else 6
        return max(0, (today.year - y) * 12 + (today.month - mo))

    out = []
    for r in rows:
        d = {
            "id": r["id"], "url": r["url"], "title": r["title"], "category": r["category"],
            "biz_model": r["biz_model"], "price": r["price"], "profit": r["profit"],
            "followers_str": r["followers_str"], "status_state": r["status_state"],
            "deal_days": r["deal_days"], "listed_at": r["listed_at"], "updated_at": r["updated_at"],
            "days_listed": _days_since(r["listed_at"]),
            "operating_months": _operating_months(r["start_date"]),
            "data_age_months": _months_since(r["updated_at"] or r["listed_at"]),
            "history_gap": ((r["monetized_months"] - _operating_months(r["start_date"]))
                            if (r["monetized_months"] is not None
                                and _operating_months(r["start_date"]) is not None) else None),
            "flags": json.loads(r["flags"] or "[]"),
            "metrics": {c: r[c] for c in _METRIC_COLS},
        }
        if r["overall_score"] is not None:
            d["evaluation"] = {
                "overall_score": r["overall_score"], "genre": r["genre"],
                "verdict": r["verdict"], "verdict_reason": r["verdict_reason"], "summary": r["summary"],
                "replication_note": r["replication_note"],
                "strengths": _load_list(r["strengths_json"]),
                "weaknesses": _load_list(r["weaknesses_json"]),
                "capability_fit": r["capability_fit"], "risk_factor": r["risk_factor"],
                "scores": {"replicability": r["replicability"], "sustainability": r["sustainability"],
                           "value": r["ev_value"], "growth": r["growth"]},
            }
        out.append(d)
    return out


def listings_for_eval(conn: sqlite3.Connection, only_id: str | None = None,
                       redo: bool = False, state: str | None = None,
                       limit: int | None = None) -> list[tuple[int, dict]]:
    """評価対象の (id, detail) を返す。既定は未評価のみ。"""
    where, params = [], []
    if only_id:
        where.append("id = ?"); params.append(int(only_id))
    if state:
        where.append("status_state = ?"); params.append(state)
    if not redo and not only_id:
        where.append("id NOT IN (SELECT listing_id FROM evaluations)")
    sql = "SELECT id, raw_json FROM listings"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY profit_recent DESC NULLS LAST"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [(r["id"], json.loads(r["raw_json"])) for r in conn.execute(sql, params).fetchall()]


def counts(conn: sqlite3.Connection) -> dict:
    by_state = dict(conn.execute("SELECT status_state, COUNT(*) FROM listings GROUP BY status_state").fetchall())
    total = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    evaluated = conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0]
    return {"total": total, "evaluated": evaluated, "by_state": by_state}


if __name__ == "__main__":
    c = init()
    print(f"DB初期化: {DB_FILE}")
    print("現在の件数:", counts(c))
