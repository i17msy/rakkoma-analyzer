#!/usr/bin/env python3
"""ラッコM&A 案件アナライザー（再現性重視のLLM評価）。

data/listings/{id}.json を読み、定量メトリクス算出 + Claudeによる評価を付与して
書き戻す。評価軸の重みは config.EVAL_WEIGHTS（再現性を最重視）。

  python3 analyze.py             # 未評価の案件すべてを評価
  python3 analyze.py --all       # 評価済みも再評価
  python3 analyze.py --id 22273  # 特定IDのみ

環境変数: ANTHROPIC_API_KEY 必須
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import anthropic

sys.path.insert(0, str(Path(__file__).parent))
from config import ANALYZER_MODEL, EVAL_WEIGHTS
import metrics as M
import storage as DB

JST = timezone(timedelta(hours=9))

# ── 評価スキーマ（構造化出力をツールで強制）─────────────────────────────────
EVAL_TOOL = {
    "name": "submit_evaluation",
    "description": "YouTubeチャンネル案件の評価結果を提出する",
    "input_schema": {
        "type": "object",
        "properties": {
            "genre": {
                "type": "string",
                "description": "ジャンル分類（例: まとめ/翻訳/解説/切り抜き/AI生成/海外反応 など。複合可）",
            },
            "scores": {
                "type": "object",
                "properties": {
                    "replicability": {
                        "type": "integer", "minimum": 1, "maximum": 5,
                        "description": "再現性: 属人性の低さ・制作の自動化/外注化のしやすさ・参入容易性。自分で同種チャンネルをゼロから作って再現できるか",
                    },
                    "sustainability": {
                        "type": "integer", "minimum": 1, "maximum": 5,
                        "description": "収益持続性: ロングテール性・プラットフォーム依存/トレンド依存/権利リスクの低さ",
                    },
                    "value": {
                        "type": "integer", "minimum": 1, "maximum": 5,
                        "description": "割安度: 回収月数・価格に対する収益の妥当性",
                    },
                    "growth": {
                        "type": "integer", "minimum": 1, "maximum": 5,
                        "description": "成長余地: 投稿頻度改善・横展開・未開拓施策などの伸びしろ",
                    },
                },
                "required": ["replicability", "sustainability", "value", "growth"],
            },
            "strengths": {"type": "array", "items": {"type": "string"}, "description": "強み（2-4個、具体的に）"},
            "weaknesses": {"type": "array", "items": {"type": "string"}, "description": "弱み・リスク（2-4個、具体的に）"},
            "replication_note": {"type": "string", "description": "自分で再現するならどう作るか／再現の難所（1-3文）"},
            "verdict": {"type": "string", "enum": ["買い", "様子見", "見送り"]},
            "verdict_reason": {"type": "string", "description": "判定理由（1-2文）"},
            "summary": {"type": "string", "description": "一言サマリ（40字以内）"},
        },
        "required": ["genre", "scores", "strengths", "weaknesses",
                     "replication_note", "verdict", "verdict_reason", "summary"],
    },
}

SYSTEM = """あなたはYouTubeチャンネルM&Aの目利きアナリストです。
評価の最重要観点は「再現性」——『この収益モデルを自分でゼロから作って再現できるか』です。

高く評価する特徴:
- 属人性が低い（顔出し・肉声なし、翻訳/まとめ/解説/AI生成で運営できる）
- 制作を自動化・外注化・テンプレ化しやすい
- ジャンルへの参入が容易で、収益がロングテール（過去動画が稼ぎ続ける）

低く評価する特徴:
- 特定個人の人気・演者・キャラに依存する
- 偶発的バズ依存で再現性がない
- 切り抜き等で権利/許諾リスクが高い、またはBANリスクがある

与えられた定量メトリクス（回収月数・収益安定度など）を必ず根拠に用い、辛口かつ具体的に
評価してください。最後に submit_evaluation ツールで結果を提出してください。"""


def _prompt(detail: dict, met: dict) -> str:
    return f"""# 案件
タイトル: {detail.get('title')}
カテゴリ: {detail.get('category')}
収益モデル: {detail.get('biz_model')}
コンテンツ性質: {detail.get('content_type')}
運営開始: {detail.get('start_date')} / 状況: {detail.get('status')} / 投稿数: {detail.get('post_count')}
登録者: {detail.get('followers_str')}
希望価格: {detail.get('price_str')}
利益/月: {detail.get('profit_str')}
説明: {detail.get('description')}

# 定量メトリクス（算出済み）
直近月利益: {met.get('profit_recent')} 円 / 平均: {met.get('profit_avg')} / 最高: {met.get('profit_max')}
回収月数（直近利益ベース）: {met.get('payback_months_recent')} ヶ月 / 平均ベース: {met.get('payback_months_avg')} ヶ月
収益安定度（直近÷平均, 1超で平均より好調・1未満で失速）: {met.get('stability')}
登録者1000人あたり月利益: {met.get('profit_per_1k_subs')} 円

上記を踏まえ、再現性を最重視して評価してください。"""


def _overall(scores: dict) -> float:
    return round(sum(scores[k] * w for k, w in EVAL_WEIGHTS.items()), 2)


def evaluate(client: anthropic.Anthropic, detail: dict):
    """1案件を評価。(metrics, evaluation) を返す。"""
    met = M.compute(detail)
    resp = client.messages.create(
        model=ANALYZER_MODEL,
        max_tokens=1500,
        system=SYSTEM,
        tools=[EVAL_TOOL],
        tool_choice={"type": "tool", "name": "submit_evaluation"},
        messages=[{"role": "user", "content": _prompt(detail, met)}],
    )
    ev = next(b.input for b in resp.content if b.type == "tool_use")
    ev["overall_score"] = _overall(ev["scores"])
    ev["model"] = ANALYZER_MODEL
    ev["evaluated_at"] = datetime.now(JST).isoformat()
    return met, ev, resp.usage


def main() -> None:
    ap = argparse.ArgumentParser(description="ラッコM&A 案件アナライザー")
    ap.add_argument("--all", action="store_true", help="評価済みも再評価する")
    ap.add_argument("--id", help="特定IDのみ評価")
    ap.add_argument("--state", choices=["募集中", "成約済み", "受付終了"], help="ステータスで絞る")
    ap.add_argument("--limit", type=int, help="先頭N件のみ（利益降順）")
    args = ap.parse_args()

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("[ERROR] 環境変数 ANTHROPIC_API_KEY が設定されていません。")
    client = anthropic.Anthropic(api_key=key)

    conn = DB.init()
    items = DB.listings_for_eval(conn, only_id=args.id, redo=args.all, state=args.state, limit=args.limit)
    print(f"評価対象: {len(items)} 件\n")

    n = in_tok = out_tok = 0
    for lid, detail in items:
        print(f"[EVAL] {lid}: {detail.get('title', '')[:44]}")
        try:
            _met, ev, usage = evaluate(client, detail)
        except Exception as e:
            print(f"  [ERROR] {e}")
            continue
        DB.save_evaluation(conn, lid, ev)
        in_tok += usage.input_tokens
        out_tok += usage.output_tokens
        print(f"  → 総合 {ev['overall_score']} / 再現性 {ev['scores']['replicability']} "
              f"/ {ev['verdict']} | {ev['summary']}")
        n += 1

    print(f"\n完了: {n}件を評価")
    if n:
        # 概算コスト（Sonnet 4.6: 入力$3 / 出力$15 per Mtok・要確認の目安）
        usd = in_tok / 1e6 * 3 + out_tok / 1e6 * 15
        print(f"トークン: 入力{in_tok:,} / 出力{out_tok:,}  "
              f"概算 ${usd:.3f}（1件あたり ${usd/n:.4f} / 入力{in_tok//n}・出力{out_tok//n}tok）")


if __name__ == "__main__":
    main()
