"""MCP 툴 구현. 서버(전송 계층)와 분리해서 테스트에서 메서드를 바로 부를 수 있게 했다.

조회 툴은 자유롭게, 주문은 preview_order → place_order 2단계 + 한도/킬스위치/모드 검사를 거친다.
"""

from __future__ import annotations

import functools
import re
import secrets
from collections.abc import Awaitable, Callable
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any, Literal, TypeVar

import httpx

from . import indicators
from .client import NotConfigured, TossClient, TossError
from .config import Settings
from .safety import Safety

SYMBOL_RE = re.compile(r"^[A-Za-z0-9.\-]{1,20}$")
SYMBOLS_RE = re.compile(r"^[A-Za-z0-9.,\-]{1,2000}$")
ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")
NUM_RE = re.compile(r"^\d+(\.\d+)?$")
MAX_CONFIRM_ATTEMPTS = 5

# 토스가 leverageFactor 를 안 내려줄 때를 대비한 대표 레버리지/인버스 ETF 목록
KNOWN_LEVERAGED = frozenset({
    "SOXL", "SOXS", "TQQQ", "SQQQ", "UPRO", "SPXU", "SPXL", "SPXS", "KORU", "TNA", "TZA", "LABU", "LABD",
    "FNGU", "FNGD", "YINN", "YANG", "TSLL", "TSLQ", "NVDL", "NVDU", "NVDD", "TECL", "TECS", "UDOW", "SDOW",
    "QLD", "QID", "SSO", "SDS", "USD", "BITX", "CONL", "MSTU", "MSTZ",
})
LEVERAGE_NAME_HINTS = ("2배", "3배", "인버스", "레버리지", "BULL 3X", "BEAR 3X", "ULTRAPRO", "2X", "3X")

F = TypeVar("F", bound=Callable[..., Awaitable[dict[str, Any]]])


def _err(e: Exception) -> dict[str, Any]:
    if isinstance(e, TossError):
        return {"ok": False, "error": e.to_dict()}
    if isinstance(e, NotConfigured):
        return {"ok": False, "error": {"code": "not-configured", "message": str(e)}}
    if isinstance(e, httpx.HTTPError):
        return {"ok": False, "error": {"code": "network", "message": f"토스 서버 연결 실패: {e.__class__.__name__}"}}
    if isinstance(e, ValueError):
        return {"ok": False, "error": {"code": "invalid-input", "message": str(e)}}
    raise e


def _fail(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}, **extra}


def safe(fn: F) -> F:
    """예상 가능한 오류(API 오류, 입력 오류, 네트워크)를 {"ok": False, ...} 로 바꾼다."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return await fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - _err 가 모르는 예외는 다시 던진다
            return _err(e)

    return wrapper  # type: ignore[return-value]


def _sym(symbol: str) -> str:
    s = symbol.strip().upper()
    if not SYMBOL_RE.match(s):
        raise ValueError(f"심볼 형식이 올바르지 않음: {symbol!r}")
    return s


def _syms(symbols: str) -> str:
    s = symbols.replace(" ", "").upper()
    if not SYMBOLS_RE.match(s):
        raise ValueError(f"심볼 목록 형식이 올바르지 않음: {symbols!r}")
    return s


def _pos_dec(v: str | None, name: str) -> Decimal | None:
    if v is None or str(v).strip() == "":
        return None
    v = str(v).strip().replace(",", "")
    if not NUM_RE.match(v):
        raise ValueError(f"{name} 은(는) 양수 숫자여야 함: {v!r}")
    try:
        d = Decimal(v)
    except InvalidOperation as exc:
        raise ValueError(f"{name} 숫자 변환 실패") from exc
    if d <= 0:
        raise ValueError(f"{name} 은(는) 0보다 커야 함")
    return d


def _fmt(v: Decimal, currency: str) -> str:
    return f"${v:,.2f}" if currency == "USD" else f"{v:,.0f}원"


def _plain(d: Decimal) -> str:
    return format(d.normalize(), "f")


def _round(obj: Any) -> Any:
    if isinstance(obj, float):
        return round(obj, 4)
    if isinstance(obj, dict):
        return {k: _round(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round(v) for v in obj]
    return obj


class Tools:
    def __init__(self, cfg: Settings, client: TossClient, safety: Safety):
        self.cfg = cfg
        self.client = client
        self.safety = safety

    # ============================================================ 시세 / 지표
    @safe
    async def quote(self, symbols: str) -> dict[str, Any]:
        """현재가 조회. symbols 는 콤마 구분 (예: 'AAPL,TSLA' 또는 '005930,000660'). 미국은 티커, 국내는 6자리 코드."""
        return {"ok": True, "result": await self.client.prices(_syms(symbols))}

    @safe
    async def orderbook(self, symbol: str) -> dict[str, Any]:
        """호가(매수/매도 잔량) 조회."""
        return {"ok": True, "result": await self.client.orderbook(_sym(symbol))}

    @safe
    async def candles(self, symbol: str, interval: Literal["1d", "1m"] = "1d", count: int = 30) -> dict[str, Any]:
        """캔들(OHLCV) 조회. interval 은 '1d'(일봉) 또는 '1m'(1분봉), count 최대 200. 최신 봉이 배열 첫 번째."""
        count = max(1, min(int(count), 200))
        return {"ok": True, "result": await self.client.candles(_sym(symbol), interval, count)}

    @safe
    async def technicals(self, symbol: str, interval: Literal["1d", "1m"] = "1d") -> dict[str, Any]:
        """기술 지표 요약: RSI(14, Wilder), 볼린저(20, 2σ), 이동평균 5/20/60, 1·5·20봉 등락률, 연속 상승/하락,
        조회 구간 고점 대비 위치. 최근 120봉 기준. 마지막 봉이 진행 중이면 실시간 값이다."""
        sym = _sym(symbol)
        data = await self.client.candles(sym, interval, 120)
        rows = list(reversed(data.get("candles", [])))  # 오래된 → 최신
        if len(rows) < 15:
            return _fail("not-enough-data", f"봉이 {len(rows)}개뿐이라 계산 불가")
        closes = [float(r["closePrice"]) for r in rows]
        highs = [float(r["highPrice"]) for r in rows]
        lows = [float(r["lowPrice"]) for r in rows]
        summary = indicators.summarize(closes, highs, lows)
        summary.update(symbol=sym, interval=interval, lastBarTime=rows[-1].get("timestamp"), currency=rows[-1].get("currency"))
        return {"ok": True, "result": _round(summary)}

    @safe
    async def stock_info(self, symbols: str) -> dict[str, Any]:
        """종목 기본 정보: 이름, 시장, 통화, 상장 상태, 레버리지 배수(leverageFactor) 등."""
        return {"ok": True, "result": await self.client.stocks(_syms(symbols))}

    @safe
    async def market_hours_us(self) -> dict[str, Any]:
        """미국장 세션 시간(데이마켓/프리/정규/애프터, 전일·당일·익일). 모든 시간 KST."""
        return {"ok": True, "result": await self.client.us_calendar()}

    @safe
    async def kr_market(self, symbols: str = "KOSPI,KOSDAQ,KR_BOND_3Y,KR_BOND_10Y") -> dict[str, Any]:
        """국내 지수/국채 금리 현재가. 지원: KOSPI, KOSDAQ, KR_BOND_2Y/3Y/5Y/10Y/20Y/30Y."""
        return {"ok": True, "result": await self.client.kr_indicators(_syms(symbols).replace(".", "_"))}

    @safe
    async def fx_usdkrw(self) -> dict[str, Any]:
        """원/달러 환율 (토스 표시 환율, 1분 갱신. 실제 체결 환율과 다를 수 있음)."""
        return {"ok": True, "result": await self.client.exchange_rate("USD", "KRW")}

    # ================================================================== 계좌
    @safe
    async def holdings(self, symbol: str | None = None) -> dict[str, Any]:
        """보유 주식과 평가손익. symbol 을 주면 그 종목만."""
        return {"ok": True, "result": await self.client.holdings(_sym(symbol) if symbol else None)}

    @safe
    async def buying_power(self, currency: Literal["USD", "KRW"] = "USD") -> dict[str, Any]:
        """현금 기준 매수 가능 금액 (미수 제외)."""
        return {"ok": True, "result": await self.client.buying_power(currency)}

    @safe
    async def orders(self, status: Literal["OPEN", "CLOSED"] = "OPEN", symbol: str | None = None, limit: int = 20) -> dict[str, Any]:
        """주문 목록. OPEN=미체결/진행 중 전체, CLOSED=종료된 주문 (limit 최대 100)."""
        lim = max(1, min(int(limit), 100)) if status == "CLOSED" else None
        return {"ok": True, "result": await self.client.list_orders(status, _sym(symbol) if symbol else None, lim)}

    @safe
    async def order_detail(self, order_id: str) -> dict[str, Any]:
        """주문 상세 (체결 수량, 평균 체결가, 수수료 등)."""
        if not ORDER_ID_RE.match(order_id or ""):
            raise ValueError("order_id 형식이 올바르지 않음")
        return {"ok": True, "result": await self.client.order_detail(order_id)}

    async def trading_status(self) -> dict[str, Any]:
        """주문 안전장치 상태: 모드(off/dry_run/live), 킬스위치, 1회·일일 한도, 오늘 사용량, 허용 종목, 확인 방식."""
        sf, c = self.safety, self.cfg
        return {
            "ok": True,
            "result": {
                "mode": sf.mode(),
                "killSwitch": sf.killed(),
                "credentialsConfigured": c.has_credentials,
                "limits": c.limits_json(),
                "today": sf.ledger().to_json(),
                "allowedSymbols": sorted(c.allowed_symbols) or "전체 허용",
                "confirmChannel": "ntfy (폰으로 확인코드)" if c.ntfy_topic else "MCP 클라이언트의 도구 승인 창",
                "recentLog": sf.recent_log(5),
            },
        }

    # ================================================================== 주문
    def _gate(self) -> dict[str, Any] | None:
        if self.safety.mode() == "off":
            return _fail("trading-off", "주문 기능이 꺼져 있음 (TRADING_MODE=off).")
        if self.safety.killed():
            return _fail("kill-switch", "킬스위치가 켜져 있어 새 주문 불가. `toss-invest-mcp kill off` 로 해제.")
        return None

    @safe
    async def preview_order(
        self,
        symbol: str,
        side: Literal["BUY", "SELL"],
        order_type: Literal["LIMIT", "MARKET", "LOC"],
        quantity: str | None = None,
        price: str | None = None,
        order_amount_usd: str | None = None,
    ) -> dict[str, Any]:
        """주문 미리보기 (실제 주문 안 나감). 한도·잔고·매도가능수량을 검사하고 preview_id 를 돌려준다.
        - order_type: LIMIT(지정가), MARKET(시장가), LOC(미국주식 장마감 지정가 = 조건 만족 시 종가에 체결)
        - quantity: 주 수량 (소수점은 미국 시장가 매도만). order_amount_usd: 금액 주문 (미국 시장가만, 정규장 중).
        - price: LIMIT/LOC 필수. LOC 매도는 '이 가격 이상이면 종가로 판다', LOC 매수는 '이 가격 이하면 종가로 산다'.
        결과 summary 와 warnings 를 사용자에게 그대로 보여주고, 사용자가 실행하라고 하면 place_order(preview_id) 를 호출한다."""
        cfg, sf = self.cfg, self.safety
        if blocked := self._gate():
            return blocked

        sym = _sym(symbol)
        if cfg.allowed_symbols and sym not in cfg.allowed_symbols:
            return _fail("symbol-not-allowed", f"{sym} 은(는) 허용 종목 목록에 없음: {sorted(cfg.allowed_symbols)}")

        qty = _pos_dec(quantity, "quantity")
        px = _pos_dec(price, "price")
        amt = _pos_dec(order_amount_usd, "order_amount_usd")
        if (qty is None) == (amt is None):
            raise ValueError("quantity 와 order_amount_usd 중 정확히 하나만 지정해야 함")

        info_list = await self.client.stocks(sym)
        if not info_list:
            raise ValueError(f"{sym} 종목을 찾지 못함")
        info = info_list[0]
        currency = info.get("currency") or ("KRW" if sym[:1].isdigit() else "USD")
        if info.get("status") not in (None, "ACTIVE"):
            return _fail("not-tradable", f"{sym} 상태가 {info.get('status')} 라서 주문 불가")
        if (info.get("koreanMarketDetail") or {}).get("krxTradingSuspended"):
            return _fail("suspended", f"{sym} 거래정지 상태")

        tif, otype = "DAY", order_type
        if order_type == "LOC":
            if currency != "USD":
                raise ValueError("LOC 는 미국 주식만 가능")
            otype, tif = "LIMIT", "CLS"
        if otype == "LIMIT" and px is None:
            raise ValueError("지정가/LOC 는 price 가 필요함")
        if otype == "MARKET" and px is not None:
            raise ValueError("시장가 주문에는 price 를 지정하면 안 됨")
        if amt is not None and not (currency == "USD" and otype == "MARKET"):
            raise ValueError("금액 주문(order_amount_usd)은 미국 시장가만 가능")
        if qty is not None and qty != qty.to_integral_value():
            if not (currency == "USD" and otype == "MARKET" and side == "SELL"):
                raise ValueError("소수점 수량은 미국 시장가 매도만 가능")
            if qty.as_tuple().exponent < -6:  # type: ignore[operator]
                raise ValueError("소수점 수량은 6자리까지")
        if px is not None:
            if currency == "KRW":
                if px != px.to_integral_value():
                    raise ValueError("국내 주식 가격은 원 단위 정수")
            else:
                px = px.quantize(Decimal("0.0001") if px < 1 else Decimal("0.01"), rounding=ROUND_DOWN)

        last_list = await self.client.prices(sym)
        last = Decimal(str(last_list[0]["lastPrice"])) if last_list and last_list[0].get("lastPrice") else None

        # 예상 금액 (한도 계산용, 보수적으로)
        if amt is not None:
            notional = amt
        elif otype == "LIMIT" and px is not None:
            ref = px
            if tif == "CLS" and side == "SELL" and last is not None:
                ref = max(px, last)  # LOC 매도는 종가로 체결 → 현재가 기준으로도 잡는다
            notional = (qty or Decimal(0)) * ref
        else:
            if last is None:
                raise ValueError("현재가를 가져오지 못해 시장가 금액을 추정할 수 없음")
            notional = (qty or Decimal(0)) * last * (1 + cfg.market_order_buffer)

        problems = sf.cap_problems(currency, notional, side)

        if side == "BUY":
            bp = await self.client.buying_power(currency)
            cash = Decimal(str(bp.get("cashBuyingPower", "0")))
            if notional > cash:
                problems.append(f"매수 가능 금액 부족: 필요 약 {_fmt(notional, currency)}, 가능 {_fmt(cash, currency)}")
        else:
            sq = await self.client.sellable_quantity(sym)
            sellable = Decimal(str(sq.get("sellableQuantity", "0")))
            if qty is not None and qty > sellable:
                problems.append(f"매도 가능 수량 부족: 요청 {qty}, 가능 {sellable}")

        warnings = self._warnings(sym, info, otype, tif, side, px, last, currency)

        if problems:
            sf.log("preview_rejected", symbol=sym, side=side, orderType=order_type, problems=problems)
            return _fail("checks-failed", " / ".join(problems), warnings=warnings)

        body: dict[str, Any] = {"symbol": sym, "side": side, "orderType": otype}
        if tif != "DAY":
            body["timeInForce"] = tif
        if qty is not None:
            body["quantity"] = _plain(qty)
        if amt is not None:
            body["orderAmount"] = _plain(amt)
        if px is not None:
            body["price"] = _plain(px)

        kind = {"LIMIT": "지정가", "MARKET": "시장가", "LOC": "LOC(장마감 지정가)"}[order_type]
        what = f"{qty}주" if qty is not None else f"${amt}어치"
        summary = (
            f"[{'실주문' if sf.mode() == 'live' else '모의(dry_run)'}] {info.get('name') or sym}({sym}) {kind} "
            f"{'매수' if side == 'BUY' else '매도'} {what}"
            + (f" @ {px}" if px is not None else "")
            + f" · 현재가 {last} · 예상 금액 약 {_fmt(notional, currency)}"
        )
        p = sf.new_preview(body, currency, notional, summary)
        pushed = await sf.push_code(p)
        sf.log("preview", previewId=p.preview_id, body=body, notional=notional)
        out: dict[str, Any] = {
            "ok": True,
            "preview_id": p.preview_id,
            "summary": summary,
            "warnings": warnings,
            "expiresInSec": cfg.preview_ttl_sec,
            "mode": sf.mode(),
            "today": sf.ledger().to_json(),
        }
        if cfg.ntfy_topic:
            out["confirm"] = (
                "확인코드를 사용자 폰(ntfy)으로 보냈음. 사용자가 채팅에 직접 입력한 6자리 코드를 place_order 의 confirm_code 로 넘길 것."
                if pushed else "⚠️ 폰으로 확인코드 전송 실패. ntfy 설정 확인 필요 (이 미리보기는 실행 불가)."
            )
        else:
            out["confirm"] = "사용자에게 summary 와 warnings 를 보여주고 '실행' 확답을 받은 뒤 place_order 호출."
        return out

    def _warnings(
        self, sym: str, info: dict[str, Any], otype: str, tif: str, side: str,
        px: Decimal | None, last: Decimal | None, currency: str,
    ) -> list[str]:
        warnings: list[str] = []
        lev = info.get("leverageFactor")
        flagged = False
        try:
            if lev is not None and abs(Decimal(str(lev))) != 1:
                warnings.append(f"⚠️ 레버리지/인버스 상품 (배수 {lev}). 하루 변동이 기초지수의 {abs(Decimal(str(lev)))}배.")
                flagged = True
        except InvalidOperation:
            pass
        name_hint = f"{info.get('name', '')} {info.get('englishName', '')}".upper()
        if not flagged and (sym in KNOWN_LEVERAGED or any(k in name_hint for k in LEVERAGE_NAME_HINTS)):
            warnings.append("⚠️ 레버리지/인버스 상품으로 보임. 오래 보유하면 횡보장에서도 가치가 줄어듦 (변동성 손실).")
        if px is not None and last and tif != "CLS":
            dev = (px / last - 1) * 100
            if abs(dev) >= 5:
                warnings.append(f"지정가가 현재가({last}) 대비 {dev:+.1f}% 떨어져 있음. 바로 체결되지 않을 수 있음.")
        if tif == "CLS":
            if side == "SELL":
                warnings.append(f"LOC 매도: 종가가 {px} 이상이면 '종가'로 체결, 미만이면 체결 안 됨.")
            else:
                warnings.append(f"LOC 매수: 종가가 {px} 이하이면 '종가'로 체결, 초과면 체결 안 됨.")
            warnings.append("LOC 접수 마감은 증권사 기준이라 장 마감 직전엔 거절될 수 있음.")
        if otype == "MARKET" and currency == "USD":
            warnings.append("미국 시장가는 데이/프리/애프터마켓에서 호가가 얇아 불리하게 체결될 수 있음.")
        return warnings

    async def place_order(self, preview_id: str, confirm_code: str | None = None) -> dict[str, Any]:
        """preview_order 로 만든 주문을 실행한다. 사용자가 미리보기를 보고 명시적으로 실행하라고 했을 때만 호출.
        확인코드(폰 전송) 설정이면 사용자가 직접 입력한 6자리 코드를 confirm_code 로 넘긴다. 같은 preview_id 는 한 번만 실행된다."""
        cfg, sf = self.cfg, self.safety
        async with sf.lock:
            if blocked := self._gate():
                return blocked
            p = sf.previews.get(preview_id or "")
            if p is None:
                return _fail("preview-not-found", "미리보기가 없음. preview_order 부터 다시 실행.")
            if p.used:
                return _fail("preview-used", "이미 실행한 미리보기 (중복 주문 방지).")
            if p.expired(cfg.preview_ttl_sec):
                return _fail("preview-expired", f"미리보기 {cfg.preview_ttl_sec}초 만료. 가격을 다시 확인하고 새로 만들 것.")
            if p.confirm_code is not None:
                if not confirm_code or not secrets.compare_digest(str(confirm_code).strip(), p.confirm_code):
                    p.failed_attempts += 1
                    sf.log("confirm_failed", previewId=p.preview_id, attempts=p.failed_attempts)
                    if p.failed_attempts >= MAX_CONFIRM_ATTEMPTS:
                        p.used = True  # 무차별 대입 방지: 미리보기 폐기
                        return _fail("bad-confirm-code", f"확인코드 {MAX_CONFIRM_ATTEMPTS}회 틀림. 이 미리보기는 폐기됨. preview_order 부터 다시.")
                    left = MAX_CONFIRM_ATTEMPTS - p.failed_attempts
                    return _fail("bad-confirm-code", f"확인코드가 틀림. 폰으로 받은 6자리를 그대로 입력 (남은 시도 {left}회).")
            if problems := sf.cap_problems(p.currency, p.est_notional, p.body["side"]):
                return _fail("limits", " / ".join(problems))

            if sf.mode() == "dry_run":
                p.used = True
                sf.log("dry_run_order", previewId=p.preview_id, body=p.body)
                return {
                    "ok": True,
                    "dryRun": True,
                    "message": "모의 모드라 실제 주문은 나가지 않음. live 모드라면 아래 주문이 토스로 전송됨.",
                    "wouldSend": {**p.body, "clientOrderId": p.preview_id},
                    "summary": p.summary,
                }

            body = {**p.body, "clientOrderId": p.preview_id}  # 멱등키: 재전송돼도 주문은 1건
            try:
                res = await self.client.create_order(body)
            except TossError as e:
                sf.log("order_error", previewId=p.preview_id, body=body, error=e.to_dict())
                return {"ok": False, "error": e.to_dict()}
            except httpx.HTTPError as e:
                sf.log("order_network_error", previewId=p.preview_id, body=body, error=e.__class__.__name__)
                return _fail(
                    "network",
                    "토스 응답을 받지 못함. 같은 preview_id 로 다시 실행해도 멱등키 때문에 중복 주문은 되지 않음(10분 이내). "
                    "먼저 orders(status='OPEN') 으로 접수 여부를 확인할 것.",
                )
            p.used = True
            sf.record(p.currency, p.est_notional, p.body["side"])
            order_id = res.get("orderId") if isinstance(res, dict) else None
            sf.log("order_placed", previewId=p.preview_id, orderId=order_id, body=body)
            detail = None
            if order_id:
                try:
                    detail = await self.client.order_detail(order_id)
                except Exception:  # noqa: BLE001 - 상세 조회 실패는 주문 성공 결과를 가리면 안 됨
                    detail = None
            return {"ok": True, "orderId": order_id, "summary": p.summary, "detail": detail, "today": sf.ledger().to_json()}

    @safe
    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """미체결 주문 취소. 위험을 줄이는 동작이라 킬스위치가 켜져 있어도 된다. dry_run 모드면 모의 취소."""
        if not ORDER_ID_RE.match(order_id or ""):
            raise ValueError("order_id 형식이 올바르지 않음")
        sf = self.safety
        if sf.mode() == "dry_run":
            sf.log("dry_run_cancel", orderId=order_id)
            return {"ok": True, "dryRun": True, "message": "모의 모드라 실제 취소는 하지 않음."}
        if sf.mode() == "off":
            return _fail("trading-off", "주문 기능이 꺼져 있음 (TRADING_MODE=off).")
        res = await self.client.cancel_order(order_id)
        sf.log("order_canceled", orderId=order_id)
        return {"ok": True, "result": res}

    async def order_log(self, n: int = 20) -> dict[str, Any]:
        """이 서버가 남긴 최근 주문 관련 로그 (미리보기, 실행, 거절, 취소)."""
        return {"ok": True, "result": self.safety.recent_log(max(1, min(int(n), 200)))}

    # ---------------------------------------------------------------- 등록용
    READ_TOOLS = (
        "quote", "orderbook", "candles", "technicals", "stock_info", "market_hours_us", "kr_market", "fx_usdkrw",
        "holdings", "buying_power", "orders", "order_detail", "trading_status", "preview_order", "order_log",
    )
    WRITE_TOOLS = ("place_order", "cancel_order")
