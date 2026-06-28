# ラッコM&A アナライザー

## プロジェクト概要

ラッコM&A（rakkoma.com）はオンライン事業の売買プラットフォーム。YouTubeチャンネル等が
匿名で売買され、Google Adsense月次収益・登録者数・再生数などが確認できる。

**目的**: 掲載案件を網羅的に収集・構造化し、**LLMが「再現性」を主軸に一次評価** →
人間はダッシュボードで横断レビューするだけ、という状態を作る。単なる新着スクレイパーでは
なく「本家より使えるアナライザー」を目指す。

### なぜ再現性重視か
MLBパイプライン（自前YouTube運営）の参考になる「継続収益があり自分で再現できるチャンネル」を
発見・蓄積したい。属人性が低く・自動化/外注しやすく・ロングテールに稼ぐモデルを高く評価する。

---

## アーキテクチャ（3層）

```
収集・列挙 ─────────────────────────────────────────────
  scrape_rakkoma.py   RSSフィード駆動の新着監視デーモン（30分間隔・Slack通知）
  enumerate_listings.py  販売中一覧巡回 + サイトマップで直近Nヶ月のクローズを列挙
  backfill.py         enum対象を巡回取得 → 詳細パース → SQLite格納（礼儀遅延つき）
        │
格納 ───┼───────────────────────────────────────────────
  storage.py (SQLite: data/rakkoma.db)
     listings      … 最新スナップショット（メトリクス列込）
     price_history … 価格・ステータス変化の履歴
     evaluations   … LLM評価（listing_idごと最新）
        │
分析・レビュー ──────────────────────────────────────────
  metrics.py     定量メトリクス算出（回収月数・収益安定度・登録者あたり利益）
  analyze.py     再現性重視のLLM評価（Claude / 構造化ツール出力）
  dashboard.py   静的HTMLダッシュボード生成（data/dashboard.html）
```

### データ規模（実測 2026-06）
| 区分 | 件数 | 取得方法 |
|---|---|---|
| 過去全件（サイトマップ） | 15,592 | `sitemap_listings.xml`（うち約9,300は2024-11-06一括=移行痕跡） |
| 直近6ヶ月の対象母数 | **約2,240** | 募集中539 + 直近クローズ1,701 |
| 販売中(active) | 539 | 一覧 `?page=1..27`（22件/頁） |

全2,240件の詳細取得は遅延3秒で約1.9時間、robots準拠10秒で約6.2時間。
LLM評価コストは1件あたり約$0.02（全2,240件で約$44 / 募集中のみ約$11）。

---

## サイト仕様（確認済み）

- **RSSフィード**: `rakkoma.com/project/list/feed`（Atom・直近約50件のスライディングウィンドウ）
- **サイトマップ**: `sitemap_listings.xml` に全案件URL（lastmod付き・全件列挙が可能）
- **詳細ページ**: ログイン不要。`<th/td>` テーブルから各フィールドを正規表現で抽出
- **ステータス判別**（詳細ページHTML内バナー）:
  | 状態 | マーカー | 付随情報 |
  |---|---|---|
  | 募集中 | バナー無し | — |
  | 成約済み | `この案件は成約済みです` | **`成約期間：N日`**（売れるまでの日数＝需要シグナル） |
  | 受付終了 | `この案件は交渉の受付を終了しています` | — |
- **robots.txt**: `Crawl-delay: 10` を要請 / `Scrapy` は全面禁止。UAは通常ブラウザ風にし礼儀遅延を入れる

### 抽出フィールド
カテゴリ / 希望売却価格 / 評価倍率(=回収月数表記) / 売上・利益（直近/平均/最高）/
運営開始時期 / 運営状況 / 投稿数 / フォロワー・登録者数 / 収益モデル / コンテンツの性質 / 説明文

### 定量メトリクス（metrics.py）
- 回収月数（直近/平均利益ベース） … price ÷ 月利益
- 収益安定度 … 直近利益 ÷ 平均利益（1超=好調 / 1未満=失速）
- 登録者1000人あたり月利益

---

## 評価仕様（analyze.py）

LLM（既定 `claude-sonnet-4-6`）が構造化ツール出力で以下を提出:
- **スコア(各1-5)**: 再現性 / 収益持続性 / 割安度 / 成長余地
- **総合スコア**: 重み付け和（再現性0.45・持続0.25・割安0.15・成長0.15 / config.EVAL_WEIGHTS）
- ジャンル分類 / 強み / 弱み・リスク / 再現メモ / 総合判定（買い・様子見・見送り）/ 一言サマリ

---

## 新着監視デーモン（scrape_rakkoma.py）

RSS駆動。新着のうち動画系タイトル → 詳細取得 → YouTube判定 → Tier判定でSlack通知。
- **Tier1（即時通知）**: 月利益 ≥ TIER1_MIN_PROFIT（既定10万円）
- **Tier2（研究候補）**: 再現性キーワード該当（翻訳/まとめ/解説/切り抜き/顔出しなし 等）

---

## ファイル構成

```
/root/rakkoma/
├── PROJECT.md
├── config.py                # 閾値・モデル・重み・マーカー等すべての設定
├── scrape_rakkoma.py        # 新着監視デーモン（RSS→Slack）
├── enumerate_listings.py    # 対象ID列挙（active巡回＋サイトマップ）
├── backfill.py              # 詳細取得→SQLite（LLM評価なし）
├── storage.py               # SQLite層（listings/price_history/evaluations）
├── metrics.py               # 定量メトリクス算出
├── analyze.py               # 再現性重視のLLM評価
├── dashboard.py             # 静的HTMLダッシュボード生成
├── run_container_rakkoma.sh # Docker起動（--restart unless-stopped で自動復帰）
└── data/
    ├── rakkoma.db           # SQLite（gitignore）
    ├── dashboard.html       # 生成物（gitignore）
    ├── enum_targets.json    # 列挙結果（gitignore）
    ├── seen_listings.json   # 既確認ID（gitignore）
    └── listings/{id}.json   # デーモンの新着保存（レガシー・将来SQLite一元化）
```

---

## 運用コマンド

```bash
# 新着監視デーモン（コンテナの常駐プロセス）
python3 scrape_rakkoma.py                 # 30分間隔ポーリング
python3 scrape_rakkoma.py --once --dry-run # 1回・通知なし

# バックフィル＆評価パイプライン
python3 enumerate_listings.py             # 対象ID確定（既定: 直近6ヶ月）
python3 backfill.py --limit 50            # 詳細取得→SQLite（パイロット）
python3 analyze.py --state 募集中 --limit 50  # LLM評価（要 ANTHROPIC_API_KEY）
python3 dashboard.py                       # ダッシュボード再生成
```

環境変数は `~/.bashrc`（ホスト）に定義し run_container 経由で `-e` 注入:
`ANTHROPIC_API_KEY`（評価）/ `SLACK_WEBHOOK_URL_RAKKOMA`（通知）。

---

## 現状（2026-06-28）

- ✅ 新着監視デーモン稼働（RSS→Tier→Slack）
- ✅ サイトマップ列挙で対象母数2,240件を確定（enum_targets.json）
- ✅ ステータス判別（成約済み/受付終了/成約期間）実装・検証
- ✅ SQLite移行（storage.py）、ダッシュボードもSQLite読み込みに統一
- ✅ LLM評価パイロット（5件）成功・コスト実測（1件$0.02）
- ⏳ データ層パイロットは55件まで投入済み

## 次のアクション

1. パイロット拡大（募集中50件→評価）で評価軸・プロンプトを微調整
2. 全2,240件のフルバックフィル（夜間・礼儀遅延）→ 全件評価
3. 新着デーモンをSQLiteに一元化（現状は data/listings JSON）
4. price_history を活かした値下げ・成約タイミング分析
5. 専用Dockerfile（軽量・rakkoma単体運用）の作成
