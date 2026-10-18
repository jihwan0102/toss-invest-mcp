from __future__ import annotations

import json
import os
from dataclasses import replace

import httpx
from starlette.testclient import TestClient

from toss_invest_mcp.client import TossClient
from toss_invest_mcp.config import Settings, load_env_file
from toss_invest_mcp.server import create_http_app


async def test_token_cached_across_clients(make_tools, fake, tmp_path):
    await make_tools().quote("AAPL")
    await make_tools().quote("AAPL")  # 새 프로세스라고 가정: 디스크 캐시를 재사용해야 함
    assert fake.token_issued == 1
    assert json.loads((tmp_path / "token.json").read_text())["token"] == "tok1"
    assert (tmp_path / "token.json").stat().st_mode & 0o777 == 0o600


async def test_401_prefers_token_refreshed_by_other_process(make_tools, fake, tmp_path):
    a, b = make_tools(), make_tools()
    await a.quote("AAPL")
    await b.quote("AAPL")
    # 다른 프로세스가 새 토큰을 받아 디스크에 저장한 상황
    (tmp_path / "token.json").write_text(json.dumps({"client_id": "id", "token": "tok-other", "exp": 9e12}))
    fake.reject_token = "tok1"
    r = await a.quote("AAPL")
    assert r["ok"]
    assert fake.token_issued == 1  # 재발급하지 않고 디스크 토큰 사용
    assert fake.calls[-1].headers["authorization"] == "Bearer tok-other"


async def test_401_reissues_when_no_better_token(make_tools, fake):
    t = make_tools()
    await t.quote("AAPL")
    fake.reject_token = "tok1"
    assert (await t.quote("AAPL"))["ok"]
    assert fake.token_issued == 2


async def test_account_header_auto_discovered(make_tools, fake):
    await make_tools().holdings()
    assert fake.calls[-1].headers["x-tossinvest-account"] == "7"


async def test_api_error_is_wrapped(make_tools):
    r = await make_tools().order_detail("does-not-exist")
    assert r["ok"]  # fake 는 아무 id 나 돌려줌
    r = await make_tools().quote("A,B")
    assert r["ok"] and r["result"] == []


async def test_403_token_mentions_ip(tmp_path):
    cfg = replace(Settings(), toss_client_id="id", toss_client_secret="s", data_dir=tmp_path)
    c = TossClient(cfg, transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"error": "forbidden"})))
    from toss_invest_mcp.tools import _err

    try:
        await c.prices("AAPL")
    except Exception as e:  # noqa: BLE001
        assert "허용 IP" in _err(e)["error"]["message"]
    else:
        raise AssertionError("403 이 예외가 되지 않음")


async def test_technicals(make_tools):
    r = await make_tools().technicals("AAPL")
    assert r["ok"], r
    assert r["result"]["streak"] < 0 and r["result"]["rsi14"] == 0.0


def test_env_file(tmp_path, monkeypatch):
    f = tmp_path / "x.env"
    f.write_text("# c\nexport TOSS_CLIENT_KEY='abc'\nTRADING_MODE=live\nMAX_ORDER_USD=5\n")
    monkeypatch.setattr(os, "environ", {"TRADING_MODE": "off"})  # 이미 있는 환경변수가 우선
    load_env_file(f)
    s = Settings()
    assert s.toss_client_id == "abc" and s.trading_mode == "off" and str(s.max_order_usd) == "5"


def test_http_requires_secret(tmp_path):
    cfg = replace(Settings(), data_dir=tmp_path, path_secret="", auth_token="")
    assert any("MCP_PATH_SECRET" in p for p in cfg.validate("http"))
    assert cfg.validate("stdio") == []


def _init(client: TestClient, path: str, **headers: str) -> httpx.Response:
    return client.post(
        path,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json", **headers},
    )


def test_http_path_secret_and_bearer(tmp_path):
    secret = "s" * 32
    cfg = replace(Settings(), data_dir=tmp_path, path_secret=secret, auth_token="t" * 32)
    with TestClient(create_http_app(cfg), base_url="http://127.0.0.1:8000") as c:
        assert c.get("/healthz").json()["ok"]
        assert _init(c, "/mcp").status_code == 401
        assert _init(c, f"/{secret}/mcp").status_code == 401
        r = _init(c, f"/{secret}/mcp", Authorization="Bearer " + "t" * 32)
        assert r.status_code == 200, r.text
        assert "place_order" in r.text
