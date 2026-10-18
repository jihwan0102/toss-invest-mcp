"""토스 Open API 를 흉내 내는 가짜 서버(httpx.MockTransport)."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import httpx
import pytest

from toss_invest_mcp.client import TossClient
from toss_invest_mcp.config import Settings
from toss_invest_mcp.safety import Safety
from toss_invest_mcp.tools import Tools

STOCKS = {
    "AAPL": {"symbol": "AAPL", "name": "애플", "currency": "USD", "status": "ACTIVE", "leverageFactor": 1},
    "SOXL": {"symbol": "SOXL", "name": "반도체 3배", "englishName": "Direxion Daily Semiconductor Bull 3X",
             "currency": "USD", "status": "ACTIVE", "leverageFactor": 3},
    "005930": {"symbol": "005930", "name": "삼성전자", "currency": "KRW", "status": "ACTIVE"},
}
PRICES = {"AAPL": "200", "SOXL": "25", "005930": "70000"}


class FakeToss:
    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.orders: list[dict[str, Any]] = []
        self.token_issued = 0
        self.cash = {"USD": "10000", "KRW": "10000000"}
        self.sellable = "10"
        self.reject_token: str | None = None

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        path, q = req.url.path, req.url.params
        if path == "/oauth2/token":
            self.token_issued += 1
            return httpx.Response(200, json={"access_token": f"tok{self.token_issued}", "expires_in": 86400})
        if self.reject_token and req.headers.get("authorization") == f"Bearer {self.reject_token}":
            return httpx.Response(401, json={"error": {"code": "unauthorized", "message": "expired"}})
        ok = lambda result: httpx.Response(200, json={"result": result})  # noqa: E731
        if path == "/api/v1/accounts":
            return ok([{"accountSeq": 7, "accountType": "BROKERAGE"}])
        if path == "/api/v1/stocks":
            return ok([STOCKS[s] for s in q["symbols"].split(",") if s in STOCKS])
        if path == "/api/v1/prices":
            return ok([{"symbol": s, "lastPrice": PRICES[s]} for s in q["symbols"].split(",") if s in PRICES])
        if path == "/api/v1/buying-power":
            return ok({"cashBuyingPower": self.cash[q["currency"]]})
        if path == "/api/v1/sellable-quantity":
            return ok({"sellableQuantity": self.sellable})
        if path == "/api/v1/orders" and req.method == "POST":
            assert req.headers["x-tossinvest-account"] == "7"
            body = json.loads(req.content)
            self.orders.append(body)
            return ok({"orderId": f"o{len(self.orders)}"})
        if path.startswith("/api/v1/orders/"):
            return ok({"orderId": path.rsplit("/", 1)[-1], "status": "FILLED"})
        if path == "/api/v1/candles":
            n = int(q["count"])
            # 최신 봉이 첫 번째. 꾸준히 하락하는 시계열
            rows = [{"closePrice": 100 + i * 0.1, "highPrice": 101, "lowPrice": 99, "timestamp": f"t{i}", "currency": "USD"} for i in range(n)]
            return ok({"candles": rows})
        return httpx.Response(404, json={"error": {"code": "not-found", "message": path}})


@pytest.fixture
def fake() -> FakeToss:
    return FakeToss()


@pytest.fixture
def make_tools(tmp_path, fake):
    def _make(**overrides: Any) -> Tools:
        base = dict(toss_client_id="id", toss_client_secret="secret", toss_account_seq="", data_dir=tmp_path,
                    ntfy_topic="", allowed_symbols=frozenset(), trading_mode="dry_run")
        cfg = replace(Settings(), **{**base, **overrides})
        client = TossClient(cfg, transport=httpx.MockTransport(fake.handler))
        return Tools(cfg, client, Safety(cfg))

    return _make
