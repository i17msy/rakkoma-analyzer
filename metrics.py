"""案件の定量メトリクス算出（LLM不要・計算のみ）。

ラッコM&Aの「評価倍率」は回収月数で表記される（price ÷ 月利益 ≒ 表示月数）ことを
確認済み。ここでは price と利益系列から回収月数・収益安定度などを算出し、
LLM評価の根拠データとして渡す。
"""

import re
import statistics as _st


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


def _series_shape(series: list[int]) -> dict:
    """生系列から"形"の指標とリスクフラグを算出（トリムしない＝立上げ初期も検知できる）。

    先頭ゼロ（収益化前）は捨てず leading_zeros として保持しつつ、CV/トレンドは収益化後の窓で
    定常値を出す。"ラッコ本体より高度に・シンプルに" 要点をフラグ化する層。
    """
    nz = [i for i, v in enumerate(series) if v > 0]
    if not nz:
        return {"flags": ["収益ゼロ"]}
    first = nz[0]
    mon = series[first:]                                  # 収益化後の窓
    mm = len(mon)
    recent, mx = series[-1], max(series)
    avg_mon = sum(mon) / mm
    recent_vs_max = round(recent / mx, 2) if mx else None
    cv = round(_st.pstdev(mon) / avg_mon, 2) if (mm >= 2 and avg_mon) else None
    # トレンド: 収益化期間の前半平均 vs 後半平均（常に重複しない窓。少ない月数でも算出可）
    h = mm // 2
    if h >= 1:
        f, l = sum(mon[:h]) / h, sum(mon[-h:]) / h
        trend = round((l / f - 1) * 100) if f > 0 else None
    else:
        trend = None
    interior_zero = any(v == 0 for v in series[first:])     # 途中ゼロ＝停止痕跡

    flags = []
    if trend is not None and trend >= 50 and recent_vs_max and recent_vs_max >= 0.8:
        flags.append("急成長×ピーク売り抜け")
    elif recent_vs_max and recent_vs_max >= 0.9:
        flags.append("ピーク売り")
    if mm <= 6:
        flags.append(f"立上げ初期{mm}ヶ月")
    if cv is not None and cv >= 0.5:
        flags.append("高変動")
    if interior_zero:
        flags.append("停止復活歴")
    if trend is not None and trend <= -25:
        flags.append("下降トレンド")
    if mm >= 12 and cv is not None and cv < 0.3 and (trend is None or trend > -15):
        flags.append("実績安定")
    return {"monetized_months": mm, "leading_zeros": first,
            "recent_vs_max": recent_vs_max, "cv": cv, "trend": trend, "flags": flags}


def compute(detail: dict) -> dict:
    """案件 detail から定量メトリクスを算出して返す。

    月次系列(profit_series)があればそれを真実の入力にする（3点要約は壊れる事がある）。
    無ければ profit_str の3点要約にフォールバック。
    """
    price = detail.get("price")
    series = [int(x) for x in (detail.get("profit_series") or []) if isinstance(x, (int, float))]
    followers = detail.get("followers")

    if series:
        months = len(series)
        recent, mx, mn = series[-1], max(series), min(series)
        nzi = [i for i, v in enumerate(series) if v > 0]
        mon = series[nzi[0]:] if nzi else []
        avg = round(sum(mon) / len(mon)) if mon else 0   # 収益化期間の平均（立上げ前ゼロを除外）
        shape = _series_shape(series)
    else:
        s = parse_profit_series(detail.get("profit_str", ""))
        recent, avg, mx, mn, months = s["recent"], s["avg"], s["max"], None, None
        shape = {}

    return {
        "profit_recent": recent,                       # 直近月利益（円）
        "profit_avg": avg,                             # 平均月利益（円）
        "profit_max": mx,                              # 最高月利益（円）
        "profit_min": mn,                              # 最低月利益（円）
        "months": months,                              # 開示された総月数
        "monetized_months": shape.get("monetized_months"),  # 収益化後の月数（実年齢）
        "leading_zeros": shape.get("leading_zeros"),        # 収益化前の月数
        "recent_vs_max": shape.get("recent_vs_max"),        # 直近÷最高（ピーク売り判定）
        "annual_profit_recent": recent * 12 if recent else None,
        "payback_months_recent": _ratio(price, recent),
        "payback_months_avg": _ratio(price, avg),
        "stability": _ratio(recent, avg),              # 勢い: 直近÷平均（方向）
        "cv": shape.get("cv"),                         # 変動: 標準偏差÷平均（ブレ）
        "trend": shape.get("trend"),                   # トレンド%: 収益化後 直近3 vs 最初3
        "flags": shape.get("flags", []),               # リスク/実績フラグ
        "profit_per_1k_subs": (
            round(recent / followers * 1000) if recent and followers else None
        ),
    }
