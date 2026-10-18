"""MCP 서버 조립: stdio(Claude Desktop/Code, Cursor 등) 또는 Streamable HTTP(claude.ai 커스텀 커넥터 등)."""

from __future__ import annotations

import logging
import secrets
import sys

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .client import TossClient
from .config import Settings
from .safety import Safety
from .tools import Tools

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)

INSTRUCTIONS = """토스증권 계좌/시세 도구 (비공식).
- 시세·지표·잔고 조회는 자유롭게 써도 된다.
- 주문은 반드시 preview_order 로 미리보기를 만들고, 그 요약을 사용자에게 그대로 보여준 뒤,
  사용자가 명시적으로 '실행'을 말했을 때만 place_order 를 호출한다. 사용자의 확인 없이 place_order 를 호출하지 않는다.
- 확인코드가 폰으로 전송되는 설정이면, 코드는 사용자가 직접 채팅에 입력한 값만 사용한다. 추측하지 않는다.
- 레버리지/인버스 ETF 경고가 나오면 사용자에게 꼭 전달한다."""


def build(cfg: Settings, transport: httpx.AsyncBaseTransport | None = None) -> tuple[MCPServer, Tools]:
    # 상태는 프로세스에 하나만 둔다. (stateless HTTP 에서는 MCP lifespan 이 요청마다 돌 수 있어서,
    #  거기서 만들면 미리보기 저장소가 요청마다 사라진다)
    tools = Tools(cfg, TossClient(cfg, transport=transport), Safety(cfg))
    # httpx 가 INFO 로 요청 URL(종목, 주문 ID)을 남기지 않도록 WARNING 으로 둔다
    mcp = MCPServer(name="toss-invest", instructions=INSTRUCTIONS, version=__version__, log_level="WARNING")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    for name in Tools.READ_TOOLS:
        mcp.tool(name=name, annotations=READ)(getattr(tools, name))
    for name in Tools.WRITE_TOOLS:
        mcp.tool(name=name, annotations=WRITE)(getattr(tools, name))

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_req: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "version": __version__, "mode": cfg.trading_mode, "credentials": cfg.has_credentials})

    return mcp, tools


class BearerAuth:
    """MCP_AUTH_TOKEN 이 설정되면 /healthz 외 모든 요청에 `Authorization: Bearer <token>` 을 요구한다."""

    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") != "/healthz":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not secrets.compare_digest(got, self.expected):
                await PlainTextResponse("unauthorized", status_code=401, headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_http_app(cfg: Settings, transport: httpx.AsyncBaseTransport | None = None) -> ASGIApp:
    problems = cfg.validate("http")
    if problems:
        raise SystemExit("설정 오류:\n- " + "\n- ".join(problems))
    mcp, _ = build(cfg, transport)
    allowed_hosts = ["127.0.0.1:*", "localhost:*"]
    if cfg.public_host:
        allowed_hosts += [cfg.public_host, f"{cfg.public_host}:*"]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=list(cfg.allowed_origins),
    )
    path = f"/{cfg.path_secret}/mcp" if cfg.path_secret else "/mcp"
    app: ASGIApp = mcp.streamable_http_app(
        streamable_http_path=path,
        stateless_http=True,
        json_response=True,
        transport_security=security,
        host=cfg.listen_host,
    )
    if cfg.auth_token:
        app = BearerAuth(app, cfg.auth_token)
    return app


def serve_http(cfg: Settings) -> None:
    import uvicorn

    app = create_http_app(cfg)
    if cfg.listen_host not in {"127.0.0.1", "localhost", "::1"}:
        print(f"⚠️  {cfg.listen_host} 에서 대기 중. HTTPS 리버스 프록시 뒤에 두는 것을 권장.", file=sys.stderr)
    # access_log 끔: 경로에 비밀값이 들어갈 수 있어서 로그에 남기지 않는다
    uvicorn.run(app, host=cfg.listen_host, port=cfg.listen_port, proxy_headers=True,
                forwarded_allow_ips="127.0.0.1", log_level="warning", access_log=False)


def serve_stdio(cfg: Settings) -> None:
    problems = cfg.validate("stdio")
    if problems:
        raise SystemExit("설정 오류:\n- " + "\n- ".join(problems))
    mcp, _ = build(cfg)
    mcp.run("stdio")
