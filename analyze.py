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
                        "description": "構造的再現性(ジャンル非依存): 属人性の低さ・制作の自動化/外注/テンプレ化のしやすさ・量産/参入容易性。特定ジャンルを優遇せず運営構造だけで判断",
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
            "capability_fit": {
                "type": "integer", "minimum": 1, "maximum": 5,
                "description": "能力適合: 評価者の『顔なし・AIナレーション動画(+隣接フォーマット)自動生成』パイプラインで再現できるか。5=直接再現可 / 3=隣接や軽い拡張で届く / 1=物販・Webサービス・実写演者必須など生産能力外。ジャンルでなく『自分が作れるか』で判断",
            },
            "strengths": {"type": "array", "items": {"type": "string"}, "description": "強み（2-4個、具体的に）"},
            "weaknesses": {"type": "array", "items": {"type": "string"}, "description": "弱み・リスク（2-4個、具体的に）"},
            "replication_note": {"type": "string", "description": "自分で再現するならどう作るか／再現の難所（1-3文）"},
            "verdict": {"type": "string", "enum": ["買い", "様子見", "見送り"]},
            "verdict_reason": {"type": "string", "description": "判定理由（1-2文）"},
            "summary": {"type": "string", "description": "一言サマリ（40字以内）"},
        },
        "required": ["genre", "scores", "capability_fit", "strengths", "weaknesses",
                     "replication_note", "verdict", "verdict_reason", "summary"],
    },
}

SYSTEM = """あなたはコンテンツ事業M&Aの目利きアナリストです。評価は独立した2軸で行います。
ジャンル名（スポーツ/解説/料理/エンタメ等）で先入観を持たず、構造だけを見てください。

【軸1: 構造的再現性（ジャンル非依存）】収益モデルを自分でゼロから再現できるかを、ジャンルに
よらず運営構造だけで評価する。
- 高評価: 属人性が低い（特定個人の人気・演者・キャラに依存しない）／制作を自動化・外注・
  テンプレ化しやすい／収益がロングテール（過去資産が稼ぎ続ける）／量産・参入が容易
- 低評価: 個人の才能やファンダムに依存／偶発的バズ依存／権利・許諾・BANリスクが高い
※ 特定ジャンルを「再現しやすい」と優遇しない。属人性も自動化可能性もジャンルでなく構造で決まる。

【軸2: 能力適合 capability_fit】評価者は「顔出し・肉声なし／AIナレーション主体の動画を自動生成
するパイプライン（およびその隣接フォーマット）」を保有する。この案件を**その生産能力で再現
できるか**を1-5で評価する。構造的に自動化可能でも、自分の生産能力で作れないものは低くする。
ジャンルでなく「自分が作れるか」で切ること。

与えられた定量メトリクス（回収月数・収益安定度など）を必ず根拠に用い、辛口かつ具体的に評価し、
submit_evaluation ツールで提出してください。"""


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

上記を踏まえ、構造的再現性（ジャンル非依存）と能力適合の2軸で評価してください。"""


def _overall(scores: dict, capability_fit: int) -> float:
    """構造スコアの重み付け和 × 能力適合ゲート(capability_fit/5)。"""
    base = sum(scores[k] * w for k, w in EVAL_WEIGHTS.items())
    return round(base * (capability_fit / 5), 2)


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
    # LLMが配列指定に反して文字列で返すことがあるため正規化
    for k in ("strengths", "weaknesses"):
        ev[k] = DB._load_list(ev.get(k) if isinstance(ev.get(k), str)
                              else json.dumps(ev.get(k), ensure_ascii=False))
    ev["overall_score"] = _overall(ev["scores"], ev["capability_fit"])
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
        print(f"  → 総合 {ev['overall_score']} / 再現{ev['scores']['replicability']} 適合{ev['capability_fit']} "
              f"/ {ev['verdict']} | [{ev['genre']}] {ev['summary']}")
        n += 1

    print(f"\n完了: {n}件を評価")
    if n:
        # 概算コスト（Sonnet 4.6: 入力$3 / 出力$15 per Mtok・要確認の目安）
        usd = in_tok / 1e6 * 3 + out_tok / 1e6 * 15
        print(f"トークン: 入力{in_tok:,} / 出力{out_tok:,}  "
              f"概算 ${usd:.3f}（1件あたり ${usd/n:.4f} / 入力{in_tok//n}・出力{out_tok//n}tok）")


if __name__ == "__main__":
    main()
