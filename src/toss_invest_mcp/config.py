"""환경변수 기반 설정.

모든 값은 환경변수(또는 `--env-file` 로 읽은 파일)에서 온다. 기본값은 "안전한 쪽"이다:
주문은 dry_run(모의), 한도는 작게, HTTP 는 로컬에서만 듣는다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path


def _env(*names: str, default: str = "") -> str:
    """앞에 있는 이름부터 찾는다 (두 번째 이후는 하위 호환 별칭)."""
    for name in names:
        val = os.environ.get(name, "").strip()
        if val:
            return val
    return default


def _dec(name: str, default: str) -> Decimal:
    return Decimal(_env(name, default=default))


_UNLIMITED = {"", "0", "off", "none", "unlimited", "무제한"}


def _limit(name: str) -> Decimal | None:
    """한도 값. 비어 있거나 0/off/unlimited 면 None(무제한)."""
    raw = _env(name).lower()
    return None if raw in _UNLIMITED else Decimal(raw)


def _limit_int(name: str) -> int | None:
    v = _limit(name)
    return None if v is None else int(v)


def _int(name: str, default: int) -> int:
    return int(_env(name, default=str(default)))


def _csv(name: str, upper: bool = True) -> tuple[str, ...]:
    items = (s.strip() for s in _env(name).split(","))
    return tuple((s.upper() if upper else s) for s in items if s)


def _default_data_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "toss-invest-mcp"


def load_env_file(path: str | Path) -> None:
    """KEY=VALUE 형식 파일을 읽어 os.environ 에 넣는다. 이미 설정된 환경변수는 덮어쓰지 않는다."""
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        os.environ.setdefault(key, val)


@dataclass(frozen=True)
class Settings:
    # 토스증권 Open API
    toss_base_url: str = field(default_factory=lambda: _env("TOSS_BASE_URL", default="https://openapi.tossinvest.com").rstrip("/"))
    toss_client_id: str = field(default_factory=lambda: _env("TOSS_CLIENT_ID", "TOSS_CLIENT_KEY"))
    toss_client_secret: str = field(default_factory=lambda: _env("TOSS_CLIENT_SECRET", "TOSS_SECRET_KEY"))
    toss_account_seq: str = field(default_factory=lambda: _env("TOSS_ACCOUNT_SEQ"))
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", default=str(_default_data_dir()))).expanduser())

    # HTTP 전송 (stdio 에서는 안 씀)
    path_secret: str = field(default_factory=lambda: _env("MCP_PATH_SECRET"))
    auth_token: str = field(default_factory=lambda: _env("MCP_AUTH_TOKEN"))
    public_host: str = field(default_factory=lambda: _env("PUBLIC_HOST"))
    listen_host: str = field(default_factory=lambda: _env("LISTEN_HOST", default="127.0.0.1"))
    listen_port: int = field(default_factory=lambda: _int("LISTEN_PORT", 8000))
    allowed_origins: tuple[str, ...] = field(
        default_factory=lambda: _csv("ALLOWED_ORIGINS", upper=False) or ("https://claude.ai", "http://localhost:*", "http://127.0.0.1:*")
    )

    # 주문 안전장치
    # off: 주문 툴 차단 / dry_run: 미리보기·모의 체결만 / live: 실제 주문
    trading_mode: str = field(default_factory=lambda: _env("TRADING_MODE", default="dry_run").lower())
    # 매수 한도. 기본은 무제한(None) — 사람이 환경변수로 직접 설정한다. 매도는 한도 대상이 아님.
    max_order_usd: Decimal | None = field(default_factory=lambda: _limit("MAX_ORDER_USD"))
    max_order_krw: Decimal | None = field(default_factory=lambda: _limit("MAX_ORDER_KRW"))
    daily_max_usd: Decimal | None = field(default_factory=lambda: _limit("DAILY_MAX_USD"))
    daily_max_krw: Decimal | None = field(default_factory=lambda: _limit("DAILY_MAX_KRW"))
    daily_max_orders: int | None = field(default_factory=lambda: _limit_int("DAILY_MAX_ORDERS"))
    allowed_symbols: frozenset[str] = field(default_factory=lambda: frozenset(_csv("ALLOWED_SYMBOLS")))
    preview_ttl_sec: int = field(default_factory=lambda: _int("PREVIEW_TTL_SEC", 300))
    market_order_buffer: Decimal = field(default_factory=lambda: _dec("MARKET_ORDER_BUFFER", "0.03"))

    # 사람 확인 채널 (선택). 설정하면 확인코드가 폰(ntfy)으로만 가고 AI 에게는 안 보인다.
    ntfy_topic: str = field(default_factory=lambda: _env("NTFY_TOPIC"))
    ntfy_server: str = field(default_factory=lambda: _env("NTFY_SERVER", default="https://ntfy.sh").rstrip("/"))

    def limits_json(self) -> dict[str, str | int]:
        def show(v: object) -> str | int:
            return "무제한" if v is None else (v if isinstance(v, int) else str(v))

        return {
            "maxOrderUsd": show(self.max_order_usd), "maxOrderKrw": show(self.max_order_krw),
            "dailyMaxUsd": show(self.daily_max_usd), "dailyMaxKrw": show(self.daily_max_krw),
            "dailyMaxOrders": show(self.daily_max_orders),
        }

    @property
    def kill_file(self) -> Path:
        return self.data_dir / "KILL"

    @property
    def has_credentials(self) -> bool:
        return bool(self.toss_client_id and self.toss_client_secret)

    def validate(self, transport: str = "stdio") -> list[str]:
        problems: list[str] = []
        if self.trading_mode not in {"off", "dry_run", "live"}:
            problems.append(f"TRADING_MODE 값이 잘못됨: {self.trading_mode!r} (off|dry_run|live)")
        if transport == "http":
            if not self.path_secret and not self.auth_token:
                problems.append("HTTP 모드에는 MCP_PATH_SECRET 또는 MCP_AUTH_TOKEN 중 하나가 필요함 (`toss-invest-mcp gen-secret`)")
            for name, val in (("MCP_PATH_SECRET", self.path_secret), ("MCP_AUTH_TOKEN", self.auth_token)):
                if val and len(val) < 16:
                    problems.append(f"{name} 가 너무 짧음 (16자 이상)")
        return problems
