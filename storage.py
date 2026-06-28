"""SQLite ストレージ層（アナライザーの分析・ダッシュボードの土台）。

テーブル:
  listings       … 案件の最新スナップショット（メトリクス列を含む）
  price_history  … 価格・ステータスの変化履歴（値下げ追跡・成約タイミング）
  evaluations    … LLM評価の最新結果（listing_id ごと1行、REPLACEで更新）

raw_json に詳細の生データを保持し、よく検索する列だけ正規化して持つハイブリッド。
"""

import json
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
    status        TEXT,          -- 詳細ページの「現在の運営状況」
    status_state  TEXT,          -- 募集中 / 成約済み / 受付終了
    deal_days     INTEGER,       -- 成約までの日数（成約済みのみ）
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
    payback_months_recent REAL,
    payback_months_avg    REAL,
    stability             REAL,
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

_METRIC_COLS = ["profit_recent", "profit_avg", "profit_max", "payback_months_recent",
                "payback_months_avg", "stability", "profit_per_1k_subs"]
_LISTING_COLS = ["url", "title", "category", "biz_model", "content_type", "status",
                 "status_state", "deal_days", "price", "price_str", "ratio_str",
                 "profit", "profit_str", "revenue", "revenue_str", "followers",
                 "followers_str", "post_count", "start_date", "description"]


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init(conn: sqlite3.Connection | None = None) -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    own = conn is None
    conn = conn or connect()
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


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
           (listing_id, overall_score, replicability, sustainability, value, growth,
            genre, verdict, verdict_reason, summary, strengths_json, weaknesses_json,
            replication_note, model, evaluated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (int(listing_id), ev.get("overall_score"), s.get("replicability"), s.get("sustainability"),
         s.get("value"), s.get("growth"), ev.get("genre"), ev.get("verdict"), ev.get("verdict_reason"),
         ev.get("summary"), json.dumps(ev.get("strengths", []), ensure_ascii=False),
         json.dumps(ev.get("weaknesses", []), ensure_ascii=False), ev.get("replication_note"),
         ev.get("model"), ev.get("evaluated_at")),
    )
    conn.commit()


def fetch_dashboard_rows(conn: sqlite3.Connection) -> list[dict]:
    """ダッシュボードJS が期待するネスト構造に整形して返す。"""
    rows = conn.execute("""
        SELECT l.*, e.overall_score, e.replicability, e.sustainability, e.value AS ev_value,
               e.growth, e.genre, e.verdict, e.verdict_reason, e.summary,
               e.strengths_json, e.weaknesses_json, e.replication_note, e.model, e.evaluated_at
        FROM listings l LEFT JOIN evaluations e ON e.listing_id = l.id
        ORDER BY e.overall_score DESC NULLS LAST, l.profit_recent DESC NULLS LAST
    """).fetchall()
    out = []
    for r in rows:
        d = {
            "id": r["id"], "url": r["url"], "title": r["title"], "category": r["category"],
            "biz_model": r["biz_model"], "price": r["price"], "profit": r["profit"],
            "followers_str": r["followers_str"], "status_state": r["status_state"],
            "deal_days": r["deal_days"],
            "metrics": {c: r[c] for c in _METRIC_COLS},
        }
        if r["overall_score"] is not None:
            d["evaluation"] = {
                "overall_score": r["overall_score"], "genre": r["genre"],
                "verdict": r["verdict"], "verdict_reason": r["verdict_reason"], "summary": r["summary"],
                "replication_note": r["replication_note"],
                "strengths": json.loads(r["strengths_json"] or "[]"),
                "weaknesses": json.loads(r["weaknesses_json"] or "[]"),
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
