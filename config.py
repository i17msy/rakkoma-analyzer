"""ラッコM&A スクレイパー設定値"""

from pathlib import Path

# ── ディレクトリ ──────────────────────────────────────────────────────────────
RAKKOMA_DIR = Path(__file__).parent
DATA_DIR    = RAKKOMA_DIR / "data"
LISTINGS_DIR = DATA_DIR / "listings"

# ── ポーリング ─────────────────────────────────────────────────────────────────
POLL_INTERVAL_SEC = 1800   # 30分

# ── RSS フィード ──────────────────────────────────────────────────────────────
FEED_URL = "https://rakkoma.com/project/list/feed"
BASE_URL  = "https://rakkoma.com"

# ── ユーザーエージェント ──────────────────────────────────────────────────────
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0"

# ── Tier 1: 即時Slack通知条件（AND）──────────────────────────────────────────
# 月次利益の最低ライン（円）
TIER1_MIN_PROFIT = 100_000   # 10万円

# ── Tier 2: 研究候補 蓄積条件 ────────────────────────────────────────────────
# 収益が小さくても「再現性あり」として保存するキーワード（タイトル or 説明文）
# 条件: YouTubeカテゴリ AND 下記いずれかに該当
REPLICABLE_CONTENT_KEYWORDS = [
    "翻訳", "まとめ", "解説", "切り抜き", "キュレーション",
    "海外反応", "海外の反応", "字幕", "吹き替え",
    "属人性なし", "非属人", "顔出しなし", "声出しなし",
]

# ── YouTube案件の一次フィルター（タイトルで詳細ページ取得をトリガー）────────
# これに1つでも引っかかれば詳細ページを取得してカテゴリ確認
YOUTUBE_TITLE_KEYWORDS = [
    "YouTube", "チャンネル", "登録者", "再生回数", "動画",
    "切り抜き", "ショート", "Vtuber", "vtuber",
]

# ── 収益モデル: アドセンス系かどうかの確認キーワード ─────────────────────────
ADSENSE_KEYWORDS = ["アドセンス", "Adsense", "AdSense", "CPM", "CPC", "収益化プログラム"]

# ── アナライザー設定 ──────────────────────────────────────────────────────────
# LLM評価モデル（評価は推論質とコストのバランスでSonnet。重い判断ならopus-4-8に）
ANALYZER_MODEL = "claude-sonnet-4-6"

# 評価軸の重み付け（再現性重視）。合計1.0
#   overall_score = Σ scores[axis] * weight[axis]
EVAL_WEIGHTS = {
    "replicability":  0.45,  # 再現性: 自分で作って再現できるか（最重視）
    "sustainability": 0.25,  # 収益持続性: ロングテール性・依存リスクの低さ
    "value":          0.15,  # 割安度: 回収月数・価格に対する収益の妥当性
    "growth":         0.15,  # 成長余地: 投稿頻度改善・未開拓施策などの伸びしろ
}

# ダッシュボード出力先
DASHBOARD_FILE = DATA_DIR / "dashboard.html"

# ── 列挙・バックフィル設定 ────────────────────────────────────────────────────
LIST_URL = f"{BASE_URL}/project/list/"                       # 販売中一覧（ページング）
SITEMAP_LISTINGS = f"{BASE_URL}/sitemap_listings.xml"        # 全案件URL（lastmod付き）
RECENT_MONTHS = 6          # サイトマップから拾うクローズ案件の鮮度（直近Nヶ月）
CRAWL_DELAY_SEC = 3.0      # 詳細ページ巡回時のリクエスト間隔（robotsはCrawl-delay:10を要請）
ENUM_TARGETS_FILE = DATA_DIR / "enum_targets.json"           # 列挙結果の確定IDリスト

# ── アイドル指定（複数コンテナ共存時の重複ポーリング防止）─────────────────────
# daemon は起動時に自分のホスト名(=コンテナ短縮ID)を照合し、下記ファイルに載っていれば
# 監視せず待機する。これにより「使い続けたい既存コンテナ」を restart してもポーリングしない。
# （環境変数 RAKKOMA_IDLE=1 でも同様にアイドル化できる＝将来コンテナを作成時に指定する用）
IDLE_HOSTS_FILE = DATA_DIR / "idle_hosts.txt"                # 監視させないホスト名（1行1件・#コメント可）

# ── ステータス判別マーカー（詳細ページHTML）──────────────────────────────────
#   募集中  : 下記いずれのバナーも無い
#   成約済み: SOLD_MARKER + 成約期間 をパース
#   受付終了: WITHDRAWN_MARKER（取り下げ・募集終了）
SOLD_MARKER = "この案件は成約済みです"
WITHDRAWN_MARKER = "この案件は交渉の受付を終了しています"
DEAL_DAYS_RE = r"成約期間：\s*(\d+)\s*日"   # 売れるまでの日数（需要シグナル）

# ── 新着の厳選通知（評価ベース・「ダッシュボードを見るべき」通知）─────────────
# 新着をLLM評価し、下記すべてを満たす案件だけSlack通知する（厳選）
NOTIFY_MIN_FIT = 4                     # 能力適合 ≥（=自分が作れる射程）
NOTIFY_MIN_OVERALL = 2.5               # 総合スコア ≥
NOTIFY_VERDICTS = ("買い", "様子見")   # この判定のみ通知（見送りは出さない）
