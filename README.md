# toss-invest-mcp

[토스증권 Open API](https://developers.tossinvest.com)를 Claude · Cursor 같은 AI 클라이언트에서 쓸 수 있게 해주는 **비공식 MCP 서버**입니다.
시세·차트·기술지표·잔고 조회는 자유롭게, **주문은 여러 겹의 안전장치**를 거쳐서만 나갑니다.

[English](README.en.md)

> ⚠️ 토스증권과 무관한 개인 프로젝트입니다. 실제 돈이 오가는 도구이니 기본값(모의 주문)으로 충분히 써본 뒤에 `live` 로 바꾸세요.
> 이 소프트웨어 사용으로 생긴 손실에 대해 작성자는 책임지지 않습니다 (MIT 라이선스).

## 할 수 있는 것

```
나: SOXL 지금 RSI 어때? 과매도면 10주 LOC 매수 미리보기 만들어줘
AI: (technicals → preview_order) RSI 27.4로 과매도 구간이에요.
    [실주문] SOXL LOC(장마감 지정가) 매수 10주 @ 24.50 · 예상 금액 약 $245.00
    ⚠️ 레버리지/인버스 상품 (배수 3) ...
    실행할까요?
```

| 분류 | 툴 |
|---|---|
| 시세 | `quote` 현재가 · `orderbook` 호가 · `candles` 일봉/분봉 · `stock_info` 종목정보 |
| 지표 | `technicals` RSI(14)·볼린저(20,2)·이평 5/20/60·등락률·연속 상승/하락 |
| 시장 | `market_hours_us` 미국장 세션 시간 · `kr_market` 코스피/코스닥/국채 · `fx_usdkrw` 환율 |
| 계좌 | `holdings` 보유·평가손익 · `buying_power` 매수가능금액 · `orders` / `order_detail` 주문내역 |
| 주문 | `preview_order` → `place_order` · `cancel_order` · `trading_status` · `order_log` |

국내(6자리 코드)와 미국(티커) 주식 모두 지원합니다. 지정가, 시장가, **LOC(장마감 지정가)**, 미국 금액 주문과 소수점 매도도 됩니다.

## 주문 안전장치

AI가 실수하거나 프롬프트 인젝션에 당해도 사고가 커지지 않도록 설계했습니다.

1. **2단계 주문**: `preview_order` 로 한도·잔고·매도가능수량을 검사한 미리보기를 먼저 만들고, 사용자가 확인한 뒤에만 `place_order(preview_id)` 를 호출합니다. 미리보기는 5분 뒤 만료되고 한 번만 실행됩니다. 확인코드를 5번 틀리면 미리보기가 폐기됩니다.
2. **모드**: `off`(주문 툴 차단) / `dry_run`(기본값, 모의 체결) / `live`(실주문).
3. **매수 한도 (선택)**: 기본은 무제한이고, 사람이 환경변수로 직접 설정합니다. 1회·일일 금액 한도(USD/KRW 각각)와 일일 매수 건수 한도를 걸 수 있고, 시장가는 현재가에 3% 버퍼를 얹어 보수적으로 계산합니다. 매도는 손절이 막히지 않도록 한도 대상이 아닙니다 (매도가능수량 검사는 그대로). AI는 한도를 바꿀 수 없습니다.
4. **허용 종목**: `ALLOWED_SYMBOLS=AAPL,TSLA` 처럼 지정하면 다른 종목은 주문할 수 없습니다.
5. **킬스위치**: `toss-invest-mcp kill on` 을 실행하면 재시작 없이 즉시 새 주문이 막힙니다. 취소는 계속 됩니다.
6. **폰 확인코드 (선택)**: `NTFY_TOPIC` 을 설정하면 6자리 코드가 [ntfy](https://ntfy.sh) 앱으로만 전송됩니다. AI는 코드를 볼 수 없어서 **사람이 직접 입력해야만** 주문이 나갑니다.
7. **멱등키**: `preview_id` 를 `clientOrderId` 로 보내므로 네트워크 오류로 재시도해도 주문은 한 번만 접수됩니다.
8. **레버리지/인버스 경고**와 현재가에서 멀리 떨어진 지정가 경고를 표시합니다.
9. 모든 미리보기·실행·거절 내역을 `orders.log.jsonl` 에 기록합니다.

## 준비

1. 토스증권 [Open API](https://developers.tossinvest.com)에서 API 키(Client ID / Secret)를 발급받습니다.
2. WTS > 설정 > Open API > **허용 IP** 에 이 서버를 실행할 컴퓨터의 공인 IP를 등록합니다. (`curl ifconfig.me`)
3. Python 3.10 이상과 [uv](https://docs.astral.sh/uv/)를 설치합니다 (pip도 됩니다).

> 토스는 Client 하나당 유효 토큰이 1개뿐입니다. 이 서버는 토큰을 `DATA_DIR/token.json` 에 캐시해서 공유하니,
> 같은 키로 다른 스크립트를 함께 돌리면 토큰이 서로를 무효화할 수 있습니다.

## 설치 & 연결

### Claude Desktop / Cursor / 그 밖의 stdio 클라이언트

```json
{
  "mcpServers": {
    "toss-invest": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/jihwan0102/toss-invest-mcp", "toss-invest-mcp"],
      "env": {
        "TOSS_CLIENT_ID": "...",
        "TOSS_CLIENT_SECRET": "...",
        "TRADING_MODE": "dry_run"
      }
    }
  }
}
```

### Claude Code

```bash
claude mcp add toss-invest \
  -e TOSS_CLIENT_ID=... -e TOSS_CLIENT_SECRET=... -e TRADING_MODE=dry_run \
  -- uvx --from git+https://github.com/jihwan0102/toss-invest-mcp toss-invest-mcp
```

### claude.ai 커스텀 커넥터 (원격 HTTP)

허용 IP가 고정된 서버(VPS 등)에서 실행합니다.

```bash
uv tool install git+https://github.com/jihwan0102/toss-invest-mcp
cp .env.example toss.env && chmod 600 toss.env      # 값 채우기
echo "MCP_PATH_SECRET=$(toss-invest-mcp gen-secret)" >> toss.env
toss-invest-mcp --env-file toss.env serve --http     # 127.0.0.1:8000
```

그다음 HTTPS 리버스 프록시(nginx, Caddy)를 앞에 두고 claude.ai > 설정 > 커넥터에
`https://<도메인>/<MCP_PATH_SECRET>/mcp` 를 등록합니다. 이 URL은 비밀번호나 마찬가지입니다.
헤더를 지원하는 클라이언트라면 `MCP_AUTH_TOKEN` 으로 Bearer 인증도 쓸 수 있습니다.
systemd 유닛과 nginx 예시는 [`deploy/`](deploy/)에 있습니다.

## 관리 명령

```bash
toss-invest-mcp check          # 키와 허용 IP 확인 (조회만 함)
toss-invest-mcp status         # 모드, 매수 한도, 오늘 사용량
toss-invest-mcp kill on|off    # 킬스위치
toss-invest-mcp logs -n 50     # 주문 로그
toss-invest-mcp gen-secret     # 비밀값 생성
```

모든 명령에 `--env-file <파일>` 을 줄 수 있습니다.

## 설정 (환경변수)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `TOSS_CLIENT_ID` / `TOSS_CLIENT_SECRET` | | 토스 Open API 키 (필수) |
| `TOSS_ACCOUNT_SEQ` | 자동 | 계좌가 여러 개일 때 지정 (기본: 첫 종합매매 계좌) |
| `TRADING_MODE` | `dry_run` | `off` / `dry_run` / `live` |
| `MAX_ORDER_USD` / `MAX_ORDER_KRW` | 무제한 | 1회 매수 한도 (비우거나 `0`/`off` 면 무제한) |
| `DAILY_MAX_USD` / `DAILY_MAX_KRW` | 무제한 | 일일 누적 매수 한도 (KST 기준) |
| `DAILY_MAX_ORDERS` | 무제한 | 일일 매수 주문 건수 |
| `ALLOWED_SYMBOLS` | 전체 | 콤마 구분 허용 종목 |
| `PREVIEW_TTL_SEC` | `300` | 미리보기 유효 시간 |
| `MARKET_ORDER_BUFFER` | `0.03` | 시장가 금액 추정 버퍼 |
| `NTFY_TOPIC` / `NTFY_SERVER` | / `https://ntfy.sh` | 폰 확인코드 (토픽은 추측 못 하게 길게) |
| `DATA_DIR` | `~/.local/state/toss-invest-mcp` | 토큰 캐시, 장부, 로그, 킬스위치 |
| `MCP_PATH_SECRET` | | HTTP 경로 비밀값 (`/<secret>/mcp`) |
| `MCP_AUTH_TOKEN` | | HTTP Bearer 토큰 |
| `LISTEN_HOST` / `LISTEN_PORT` | `127.0.0.1` / `8000` | HTTP 대기 주소 |
| `PUBLIC_HOST` | | 프록시 뒤 공개 호스트명 (Host 헤더 검사용) |
| `ALLOWED_ORIGINS` | claude.ai, localhost | 허용 Origin |

## 개발

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/pytest && .venv/bin/ruff check .
```

테스트는 토스 API를 `httpx.MockTransport` 로 흉내 내므로 키 없이 돌아갑니다.

## 라이선스

MIT
