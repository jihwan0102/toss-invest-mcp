"""기술 지표 계산. 입력은 오래된 것부터 최신 순서의 종가 리스트."""

from __future__ import annotations

import math
from statistics import fmean, pstdev


def sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return fmean(values[-n:])


def rsi_wilder(closes: list[float], n: int = 14) -> float | None:
    """Wilder 방식 RSI. 토스/트레이딩뷰 기본값과 같은 방식."""
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for prev, cur in zip(closes[:-1], closes[1:]):
        diff = cur - prev
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = fmean(gains[:n])
    avg_loss = fmean(losses[:n])
    for gain, loss in zip(gains[n:], losses[n:]):
        avg_gain = (avg_gain * (n - 1) + gain) / n
        avg_loss = (avg_loss * (n - 1) + loss) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def bollinger(closes: list[float], n: int = 20, k: float = 2.0) -> dict[str, float] | None:
    """모집단 표준편차 기준 (트레이딩뷰 기본과 동일)."""
    if len(closes) < n:
        return None
    window = closes[-n:]
    mid = fmean(window)
    sd = pstdev(window)
    upper, lower = mid + k * sd, mid - k * sd
    last = closes[-1]
    width = upper - lower
    pct_b = (last - lower) / width if width else math.nan
    return {"upper": upper, "middle": mid, "lower": lower, "percentB": pct_b, "bandwidthPct": (width / mid * 100) if mid else math.nan}


def pct_change(closes: list[float], bars: int) -> float | None:
    if len(closes) <= bars or closes[-1 - bars] == 0:
        return None
    return (closes[-1] / closes[-1 - bars] - 1.0) * 100.0


def consecutive_streak(closes: list[float]) -> int:
    """양수면 연속 상승 봉 수, 음수면 연속 하락 봉 수."""
    streak = 0
    for prev, cur in zip(reversed(closes[:-1]), reversed(closes[1:])):
        if cur > prev:
            if streak < 0:
                break
            streak += 1
        elif cur < prev:
            if streak > 0:
                break
            streak -= 1
        else:
            break
    return streak


def summarize(closes: list[float], highs: list[float], lows: list[float]) -> dict:
    rsi = rsi_wilder(closes)
    bb = bollinger(closes)
    out: dict = {
        "last": closes[-1] if closes else None,
        "bars": len(closes),
        "rsi14": rsi,
        "bollinger20_2": bb,
        "sma5": sma(closes, 5),
        "sma20": sma(closes, 20),
        "sma60": sma(closes, 60),
        "change1Pct": pct_change(closes, 1),
        "change5Pct": pct_change(closes, 5),
        "change20Pct": pct_change(closes, 20),
        "streak": consecutive_streak(closes),
        "highOfRange": max(highs) if highs else None,
        "lowOfRange": min(lows) if lows else None,
    }
    if out["highOfRange"]:
        out["fromHighPct"] = (closes[-1] / out["highOfRange"] - 1.0) * 100.0
    notes = []
    if rsi is not None:
        if rsi <= 30:
            notes.append("RSI 30 이하: 과매도 구간")
        elif rsi >= 70:
            notes.append("RSI 70 이상: 과매수 구간")
    if bb:
        if closes[-1] < bb["lower"]:
            notes.append("종가가 볼린저 하단 밖: 강한 하락 추세 or 과매도")
        elif closes[-1] > bb["upper"]:
            notes.append("종가가 볼린저 상단 밖: 강한 상승 추세 or 과매수")
    if out["streak"] <= -5:
        notes.append(f"{-out['streak']}봉 연속 하락")
    elif out["streak"] >= 5:
        notes.append(f"{out['streak']}봉 연속 상승")
    out["notes"] = notes
    return out
