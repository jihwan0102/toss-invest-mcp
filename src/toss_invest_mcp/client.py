"""토스증권 Open API 비동기 클라이언트.

- OAuth2 client_credentials 토큰을 디스크에 캐시한다. 토스는 client 당 유효 토큰이 1개라서,
  프로세스마다 새로 발급하면 다른 곳(다른 MCP 프로세스, 스크립트)에서 쓰던 토큰이 무효화된다.
- 401 이면 먼저 디스크 캐시를 다시 읽고(다른 프로세스가 갱신했을 수 있음), 그래도 같으면 재발급.
- 429 면 짧게 백오프 후 재시도.
- 응답 envelope `{"result": ...}` 를 벗겨서 result 만 돌려준다.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

from . import __version__
from .config import Settings


class TossError(Exception):
    def __init__(self, status: int, code: str, message: str, data: Any = None, request_id: str = ""):
        self.status = status
        self.code = code
        self.message = message
        self.data = data
        self.request_id = request_id
        super().__init__(f"[{status} {code}] {message}")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"httpStatus": self.status, "code": self.code, "message": self.message}
        if self.data:
            out["data"] = self.data
        if self.request_id:
            out["requestId"] = self.request_id
        return out


class NotConfigured(Exception):
    pass


_IP_HINT = " (403 이면 대부분 토스 WTS > 설정 > Open API 의 '허용 IP' 에 이 서버의 공인 IP 가 등록되지 않은 경우)"


class TossClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.s = settings
        self._http = httpx.AsyncClient(
            base_url=settings.toss_base_url,
            timeout=httpx.Timeout(10.0, connect=5.0),
            transport=transport,
            headers={"User-Agent": f"toss-invest-mcp/{__version__}"},
        )
        self._token: str | None = None
        self._token_exp: float = 0.0
        self._token_lock = asyncio.Lock()
        self._account_seq: str | None = settings.toss_account_seq or None
        self._token_file: Path = settings.data_dir / "token.json"

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ token
    def _load_cached_token(self) -> bool:
        try:
            raw = json.loads(self._token_file.read_text())
            if raw.get("client_id") == self.s.toss_client_id and raw.get("exp", 0) > time.time() + 300:
                self._token, self._token_exp = raw["token"], float(raw["exp"])
                return True
        except (OSError, ValueError, KeyError):
            pass
        return False

    def _save_token(self) -> None:
        try:
            self._token_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._token_file.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"client_id": self.s.toss_client_id, "token": self._token, "exp": self._token_exp}, f)
            os.replace(tmp, self._token_file)
        except OSError:
            pass  # 캐시는 최적화일 뿐, 실패해도 동작에는 지장 없음

    async def _get_token(self, stale: str | None = None) -> str:
        """유효 토큰을 돌려준다. stale 을 주면 그 토큰은 거절된 것으로 보고 다른 토큰을 구한다."""
        if not self.s.has_credentials:
            raise NotConfigured("토스 API 키가 설정되지 않음. 환경변수 TOSS_CLIENT_ID, TOSS_CLIENT_SECRET 을 넣고 서버를 다시 시작하세요.")
        async with self._token_lock:
            if stale is None and self._token and self._token_exp > time.time() + 300:
                return self._token
            if self._load_cached_token() and self._token != stale:
                return self._token  # type: ignore[return-value]
            resp = await self._http.post(
                "/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.s.toss_client_id,
                    "client_secret": self.s.toss_client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code != 200:
                try:
                    err = resp.json()
                except ValueError:
                    err = {}
                raise TossError(
                    resp.status_code,
                    err.get("error", "token-error"),
                    (err.get("error_description") or "토큰 발급 실패") + (_IP_HINT if resp.status_code == 403 else ""),
                )
            body = resp.json()
            self._token = body["access_token"]
            self._token_exp = time.time() + int(body.get("expires_in", 86400))
            self._save_token()
            return self._token  # type: ignore[return-value]

    # ---------------------------------------------------------------- request
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        account: bool = False,
    ) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        refreshed = False
        token = await self._get_token()
        for attempt in range(4):
            headers = {"Authorization": f"Bearer {token}"}
            if account:
                headers["X-Tossinvest-Account"] = await self.account_seq()
            resp = await self._http.request(method, path, params=params, json=json_body, headers=headers)
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                token = await self._get_token(stale=token)
                continue
            if resp.status_code == 429 and attempt < 3:
                retry_after = resp.headers.get("Retry-After", "")
                delay = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else 0.5 * (2**attempt)
                await asyncio.sleep(min(delay, 3.0))
                continue
            if resp.status_code >= 400:
                try:
                    err = resp.json().get("error", {})
                except (ValueError, AttributeError):
                    err = {}
                raise TossError(
                    resp.status_code,
                    err.get("code", "http-error"),
                    (err.get("message") or resp.reason_phrase or "요청 실패") + (_IP_HINT if resp.status_code == 403 else ""),
                    err.get("data"),
                    err.get("requestId", resp.headers.get("X-Request-Id", "")),
                )
            body = resp.json() if resp.content else {}
            return body.get("result", body) if isinstance(body, dict) else body
        raise TossError(429, "rate-limited", "호출 한도 초과가 계속됨. 잠시 후 다시 시도하세요.")

    async def account_seq(self) -> str:
        if self._account_seq:
            return self._account_seq
        accounts = await self.accounts()
        brokerage = [a for a in accounts if a.get("accountType") == "BROKERAGE"] or accounts
        if not brokerage:
            raise TossError(404, "no-account", "Open API 로 쓸 수 있는 종합매매 계좌가 없음.")
        self._account_seq = str(brokerage[0]["accountSeq"])
        return self._account_seq

    # ------------------------------------------------------------ endpoints
    async def accounts(self) -> list[dict[str, Any]]:
        return await self.request("GET", "/api/v1/accounts") or []

    async def prices(self, symbols: str) -> Any:
        return await self.request("GET", "/api/v1/prices", params={"symbols": symbols})

    async def orderbook(self, symbol: str) -> Any:
        return await self.request("GET", "/api/v1/orderbook", params={"symbol": symbol})

    async def candles(self, symbol: str, interval: str, count: int, before: str | None = None) -> Any:
        return await self.request(
            "GET", "/api/v1/candles", params={"symbol": symbol, "interval": interval, "count": count, "before": before}
        )

    async def stocks(self, symbols: str) -> Any:
        return await self.request("GET", "/api/v1/stocks", params={"symbols": symbols})

    async def exchange_rate(self, base: str = "USD", quote: str = "KRW") -> Any:
        return await self.request("GET", "/api/v1/exchange-rate", params={"baseCurrency": base, "quoteCurrency": quote})

    async def us_calendar(self, date: str | None = None) -> Any:
        return await self.request("GET", "/api/v1/market-calendar/US", params={"date": date})

    async def kr_indicators(self, symbols: str) -> Any:
        return await self.request("GET", "/api/v1/market-indicators/prices", params={"symbols": symbols})

    async def holdings(self, symbol: str | None = None) -> Any:
        return await self.request("GET", "/api/v1/holdings", params={"symbol": symbol}, account=True)

    async def buying_power(self, currency: str) -> Any:
        return await self.request("GET", "/api/v1/buying-power", params={"currency": currency}, account=True)

    async def sellable_quantity(self, symbol: str) -> Any:
        return await self.request("GET", "/api/v1/sellable-quantity", params={"symbol": symbol}, account=True)

    async def list_orders(self, status: str, symbol: str | None, limit: int | None) -> Any:
        return await self.request(
            "GET", "/api/v1/orders", params={"status": status, "symbol": symbol, "limit": limit}, account=True
        )

    async def order_detail(self, order_id: str) -> Any:
        return await self.request("GET", f"/api/v1/orders/{order_id}", account=True)

    async def create_order(self, body: dict[str, Any]) -> Any:
        return await self.request("POST", "/api/v1/orders", json_body=body, account=True)

    async def cancel_order(self, order_id: str) -> Any:
        return await self.request("POST", f"/api/v1/orders/{order_id}/cancel", json_body={}, account=True)
