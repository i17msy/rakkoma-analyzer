"""案件の定量メトリクス算出（LLM不要・計算のみ）。

ラッコM&Aの「評価倍率」は回収月数で表記される（price ÷ 月利益 ≒ 表示月数）ことを
確認済み。ここでは price と利益系列から回収月数・収益安定度などを算出し、
LLM評価の根拠データとして渡す。
"""

import re


def _money(s: str):
    """文字列から最初の金額（カンマ区切り）を整数で抽出。無ければ None。"""
    m = re.search(r"[\d,]+", s or "")
    return int(m.group().replace(",", "")) if m else None


def parse_profit_series(s: str) -> dict:
    """ '¥186,707 平均 ¥292,417 / 最高 ¥567,777' → 直近/平均/最高 を分解。"""
    s = s or ""
    recent = _money(s.split("平均")[0]) if s else None
    avg_part = s.split("平均", 1)[1] if "平均" in s else ""
    avg = _money(avg_part.split("最高")[0]) if avg_part else None
    mx = _money(s.split("最高", 1)[1]) if "最高" in s else None
    return {"recent": recent, "avg": avg, "max": mx}


def _ratio(a, b, nd=2):
    return round(a / b, nd) if a and b else None


def compute(detail: dict) -> dict:
    """案件 detail から定量メトリクスを算出して返す。"""
    price = detail.get("price")
    series = parse_profit_series(detail.get("profit_str", ""))
    recent, avg, mx = series["recent"], series["avg"], series["max"]
    followers = detail.get("followers")

    return {
        "profit_recent": recent,                       # 直近月利益（円）
        "profit_avg": avg,                             # 平均月利益（円）
        "profit_max": mx,                              # 最高月利益（円）
        "annual_profit_recent": recent * 12 if recent else None,
        "payback_months_recent": _ratio(price, recent),  # 回収月数（直近利益ベース）
        "payback_months_avg": _ratio(price, avg),        # 回収月数（平均利益ベース）
        "stability": _ratio(recent, avg),                # 収益安定度: 1超=平均より好調 / 1未満=失速
        "profit_per_1k_subs": (
            round(recent / followers * 1000) if recent and followers else None
        ),                                               # 登録者1000人あたり月利益（円）
    }
