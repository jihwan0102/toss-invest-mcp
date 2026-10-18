from __future__ import annotations

import time
from decimal import Decimal


async def test_dry_run_never_sends(make_tools, fake):
    t = make_tools(trading_mode="dry_run")
    p = await t.preview_order("aapl", "BUY", "LIMIT", quantity="2", price="199.999")
    assert p["ok"], p
    assert "모의" in p["summary"]
    r = await t.place_order(p["preview_id"])
    assert r["ok"] and r["dryRun"]
    assert r["wouldSend"] == {"symbol": "AAPL", "side": "BUY", "orderType": "LIMIT", "quantity": "2", "price": "199.99",
                              "clientOrderId": p["preview_id"]}
    assert fake.orders == []


async def test_live_places_once_with_idempotency_key(make_tools, fake):
    t = make_tools(trading_mode="live")
    p = await t.preview_order("AAPL", "BUY", "LIMIT", quantity="1", price="190")
    r = await t.place_order(p["preview_id"])
    assert r["ok"] and r["orderId"] == "o1"
    assert fake.orders == [{"symbol": "AAPL", "side": "BUY", "orderType": "LIMIT", "quantity": "1", "price": "190",
                            "clientOrderId": p["preview_id"]}]
    again = await t.place_order(p["preview_id"])
    assert again["error"]["code"] == "preview-used"
    assert len(fake.orders) == 1
    assert t.safety.ledger().orders == 1


async def test_off_mode_and_kill_switch(make_tools, tmp_path):
    t = make_tools(trading_mode="off")
    assert (await t.preview_order("AAPL", "BUY", "MARKET", quantity="1"))["error"]["code"] == "trading-off"

    t = make_tools(trading_mode="live")
    p = await t.preview_order("AAPL", "BUY", "MARKET", quantity="1")
    assert p["ok"]
    (tmp_path / "KILL").touch()
    assert (await t.place_order(p["preview_id"]))["error"]["code"] == "kill-switch"


async def test_per_order_limit(make_tools):
    t = make_tools(max_order_usd=Decimal("100"))
    p = await t.preview_order("AAPL", "BUY", "LIMIT", quantity="1", price="150")
    assert p["error"]["code"] == "checks-failed"
    assert "1회 매수 한도" in p["error"]["message"]


async def test_market_order_uses_buffer(make_tools):
    # 200 * 5 * 1.03 = 1030 > 1000
    t = make_tools(max_order_usd=Decimal("1000"))
    p = await t.preview_order("AAPL", "BUY", "MARKET", quantity="5")
    assert p["error"]["code"] == "checks-failed"


async def test_daily_order_count(make_tools):
    t = make_tools(trading_mode="live", daily_max_orders=1)
    p1 = await t.preview_order("AAPL", "BUY", "LIMIT", quantity="1", price="190")
    assert (await t.place_order(p1["preview_id"]))["ok"]
    p2 = await t.preview_order("AAPL", "BUY", "LIMIT", quantity="1", price="190")
    assert "매수 주문 횟수 한도" in p2["error"]["message"]


async def test_allowed_symbols(make_tools):
    t = make_tools(allowed_symbols=frozenset({"SOXL"}))
    assert (await t.preview_order("AAPL", "BUY", "LIMIT", quantity="1", price="1"))["error"]["code"] == "symbol-not-allowed"


async def test_insufficient_cash_and_sellable(make_tools, fake):
    fake.cash["USD"] = "100"
    fake.sellable = "1"
    t = make_tools()
    assert "매수 가능 금액 부족" in (await t.preview_order("AAPL", "BUY", "LIMIT", quantity="1", price="190"))["error"]["message"]
    assert "매도 가능 수량 부족" in (await t.preview_order("AAPL", "SELL", "LIMIT", quantity="2", price="190"))["error"]["message"]


async def test_preview_expires(make_tools):
    t = make_tools(trading_mode="live", preview_ttl_sec=1)
    p = await t.preview_order("AAPL", "BUY", "LIMIT", quantity="1", price="190")
    t.safety.previews[p["preview_id"]].created = time.time() - 5
    assert (await t.place_order(p["preview_id"]))["error"]["code"] == "preview-expired"


async def test_confirm_code_required(make_tools, monkeypatch):
    t = make_tools(trading_mode="live", ntfy_topic="some-long-topic-name")

    async def fake_push(p):
        return True

    monkeypatch.setattr(t.safety, "push_code", fake_push)
    p = await t.preview_order("AAPL", "BUY", "LIMIT", quantity="1", price="190")
    assert "ntfy" in p["confirm"]
    assert (await t.place_order(p["preview_id"]))["error"]["code"] == "bad-confirm-code"
    wrong = "000000" if t.safety.previews[p["preview_id"]].confirm_code != "000000" else "111111"
    assert (await t.place_order(p["preview_id"], wrong))["error"]["code"] == "bad-confirm-code"
    code = t.safety.previews[p["preview_id"]].confirm_code
    assert (await t.place_order(p["preview_id"], code))["ok"]


async def test_loc_and_leverage_warning(make_tools):
    t = make_tools()
    p = await t.preview_order("SOXL", "BUY", "LOC", quantity="3", price="24.5")
    assert p["ok"], p
    assert any("레버리지" in w for w in p["warnings"])
    assert any("LOC 매수" in w for w in p["warnings"])
    body = t.safety.previews[p["preview_id"]].body
    assert body == {"symbol": "SOXL", "side": "BUY", "orderType": "LIMIT", "timeInForce": "CLS", "quantity": "3", "price": "24.5"}


async def test_input_validation(make_tools):
    t = make_tools()
    assert (await t.preview_order("AAPL", "BUY", "LIMIT", quantity="1"))["error"]["code"] == "invalid-input"
    assert (await t.preview_order("AAPL", "BUY", "MARKET", quantity="1", price="1"))["error"]["code"] == "invalid-input"
    assert (await t.preview_order("AAPL", "BUY", "LIMIT", quantity="1.5", price="1"))["error"]["code"] == "invalid-input"
    assert (await t.preview_order("005930", "BUY", "LIMIT", quantity="1", price="70000.5"))["error"]["code"] == "invalid-input"
    assert (await t.preview_order("005930", "BUY", "LOC", quantity="1", price="70000"))["error"]["code"] == "invalid-input"
    assert (await t.preview_order("AAPL;rm", "BUY", "MARKET", quantity="1"))["error"]["code"] == "invalid-input"
    assert (await t.preview_order("AAPL", "BUY", "MARKET", quantity="1", order_amount_usd="10"))["error"]["code"] == "invalid-input"


async def test_cancel_respects_mode(make_tools, fake):
    assert (await make_tools(trading_mode="dry_run").cancel_order("o1"))["dryRun"]
    assert (await make_tools(trading_mode="off").cancel_order("o1"))["error"]["code"] == "trading-off"


async def test_confirm_code_bruteforce_burns_preview(make_tools, monkeypatch):
    t = make_tools(trading_mode="live", ntfy_topic="some-long-topic-name")

    async def fake_push(p):
        return True

    monkeypatch.setattr(t.safety, "push_code", fake_push)
    p = await t.preview_order("AAPL", "BUY", "LIMIT", quantity="1", price="190")
    code = t.safety.previews[p["preview_id"]].confirm_code
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        assert (await t.place_order(p["preview_id"], wrong))["error"]["code"] == "bad-confirm-code"
    assert (await t.place_order(p["preview_id"], code))["error"]["code"] == "preview-used"


async def test_sells_are_exempt_from_limits(make_tools):
    t = make_tools(trading_mode="live", max_order_usd=Decimal("100"), daily_max_usd=Decimal("100"), daily_max_orders=1)
    for _ in range(2):  # 1회 한도($100)·일일 건수(1건)를 넘어도 매도는 됨
        p = await t.preview_order("AAPL", "SELL", "LIMIT", quantity="5", price="200")
        assert p["ok"], p
        assert (await t.place_order(p["preview_id"]))["ok"]
    assert t.safety.ledger().orders == 0
    b = await t.preview_order("AAPL", "BUY", "LIMIT", quantity="1", price="150")
    assert "1회 매수 한도" in b["error"]["message"]


async def test_buy_limits_unlimited_by_default(make_tools):
    t = make_tools()
    assert t.cfg.max_order_usd is None and t.cfg.daily_max_orders is None
    p = await t.preview_order("AAPL", "BUY", "LIMIT", quantity="40", price="200")  # $8,000
    assert p["ok"], p
    assert (await t.trading_status())["result"]["limits"]["maxOrderUsd"] == "무제한"


def test_limit_env_parsing(monkeypatch):
    from toss_invest_mcp.config import Settings

    monkeypatch.setenv("MAX_ORDER_USD", "500")
    monkeypatch.setenv("DAILY_MAX_USD", "off")
    monkeypatch.setenv("DAILY_MAX_ORDERS", "3")
    s = Settings()
    assert s.max_order_usd == Decimal("500") and s.daily_max_usd is None and s.daily_max_orders == 3
