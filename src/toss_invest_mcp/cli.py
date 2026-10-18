"""명령행 진입점.

    toss-invest-mcp                     # stdio 서버 (기본)
    toss-invest-mcp serve --http        # Streamable HTTP 서버
    toss-invest-mcp status | check | kill on|off | logs | gen-secret
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys

from . import __version__
from .config import Settings, load_env_file


def _status(cfg: Settings) -> int:
    from .safety import Safety

    sf = Safety(cfg)
    print(f"버전     : {__version__}")
    print(f"모드     : {cfg.trading_mode}")
    print(f"킬스위치 : {'켜짐 (새 주문 차단)' if sf.killed() else '꺼짐'}")
    print(f"API 키   : {'설정됨' if cfg.has_credentials else '없음 (TOSS_CLIENT_ID / TOSS_CLIENT_SECRET)'}")
    lim = cfg.limits_json()
    print(f"매수한도 : 1회 {lim['maxOrderUsd']} USD / {lim['maxOrderKrw']} KRW, "
          f"일일 {lim['dailyMaxUsd']} USD / {lim['dailyMaxKrw']} KRW, {lim['dailyMaxOrders']}건")
    print(f"허용종목 : {','.join(sorted(cfg.allowed_symbols)) or '전체'}")
    print(f"확인방식 : {'ntfy 확인코드' if cfg.ntfy_topic else '클라이언트 도구 승인'}")
    print(f"오늘     : {json.dumps(sf.ledger().to_json(), ensure_ascii=False)}")
    print(f"데이터   : {cfg.data_dir}")
    return 0


def _check(cfg: Settings) -> int:
    """키·IP 허용 여부를 실제 호출로 확인한다 (조회만, 주문 없음)."""
    from .client import NotConfigured, TossClient, TossError

    async def run() -> int:
        client = TossClient(cfg)
        try:
            accounts = await client.accounts()
            print(f"✅ 인증 성공. 계좌 {len(accounts)}개: " + ", ".join(f"{a.get('accountType')}#{a.get('accountSeq')}" for a in accounts))
            q = await client.prices("AAPL")
            print(f"✅ 시세 조회 성공: AAPL {q[0].get('lastPrice') if q else '?'}")
            return 0
        except (TossError, NotConfigured) as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1
        finally:
            await client.aclose()

    return asyncio.run(run())


def _kill(cfg: Settings, state: str) -> int:
    cfg.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if state == "on":
        cfg.kill_file.touch()
        print("킬스위치 켜짐: 새 주문 즉시 차단 (재시작 불필요)")
    else:
        cfg.kill_file.unlink(missing_ok=True)
        print("킬스위치 꺼짐")
    return 0


def _logs(cfg: Settings, n: int) -> int:
    from .safety import Safety

    for rec in Safety(cfg).recent_log(n):
        print(json.dumps(rec, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="toss-invest-mcp", description="토스증권 Open API MCP 서버 (비공식)")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--env-file", help="KEY=VALUE 설정 파일 (이미 설정된 환경변수가 우선)")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("serve", help="MCP 서버 실행 (기본 stdio)")
    sp.add_argument("--http", action="store_true", help="Streamable HTTP 로 실행")
    sub.add_parser("status", help="안전장치 설정과 오늘 사용량")
    sub.add_parser("check", help="API 키·허용 IP 확인 (조회 호출만)")
    kp = sub.add_parser("kill", help="킬스위치 (새 주문 즉시 차단)")
    kp.add_argument("state", choices=["on", "off"])
    lp = sub.add_parser("logs", help="최근 주문 로그")
    lp.add_argument("-n", type=int, default=30)
    sub.add_parser("gen-secret", help="MCP_PATH_SECRET / MCP_AUTH_TOKEN 용 난수 생성")

    args = p.parse_args(argv)
    if args.cmd == "gen-secret":
        print(secrets.token_urlsafe(32))
        return 0
    if args.env_file:
        load_env_file(args.env_file)
    cfg = Settings()

    if args.cmd in (None, "serve"):
        from .server import serve_http, serve_stdio

        (serve_http if getattr(args, "http", False) else serve_stdio)(cfg)
        return 0
    if args.cmd == "status":
        return _status(cfg)
    if args.cmd == "check":
        return _check(cfg)
    if args.cmd == "kill":
        return _kill(cfg, args.state)
    if args.cmd == "logs":
        return _logs(cfg, args.n)
    p.error(f"알 수 없는 명령: {args.cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
