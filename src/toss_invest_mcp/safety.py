"""주문 안전장치: 미리보기 저장소, 일일 한도 장부, 킬스위치, 주문 로그, 폰 확인코드.

장부·킬스위치·로그는 DATA_DIR 의 파일이라 여러 프로세스(stdio 클라이언트 여러 개)가 공유한다.
미리보기는 프로세스 메모리에만 있어서, 미리보기를 만든 프로세스에서만 실행할 수 있다.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx

from .config import Settings

KST = timezone(timedelta(hours=9))


def today_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


@dataclass
class Preview:
    preview_id: str
    created: float
    body: dict[str, Any]          # 토스에 그대로 보낼 주문 본문 (clientOrderId 는 실행 때 붙임)
    currency: str                 # USD | KRW
    est_notional: Decimal         # 한도 계산에 쓰는 예상 금액
    summary: str
    confirm_code: str | None = None
    used: bool = False
    failed_attempts: int = 0

    def expired(self, ttl: int) -> bool:
        return time.time() - self.created > ttl


@dataclass
class Ledger:
    date: str
    orders: int = 0
    usd: Decimal = Decimal("0")
    krw: Decimal = Decimal("0")

    def to_json(self) -> dict[str, Any]:
        return {"date": self.date, "orders": self.orders, "usd": str(self.usd), "krw": str(self.krw)}


class Safety:
    def __init__(self, settings: Settings):
        self.s = settings
        self.previews: dict[str, Preview] = {}
        self.lock = asyncio.Lock()
        self.s.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._ledger_file = self.s.data_dir / "ledger.json"
        self._log_file = self.s.data_dir / "orders.log.jsonl"

    # ------------------------------------------------------------- status
    def killed(self) -> bool:
        return self.s.kill_file.exists()

    def mode(self) -> str:
        return self.s.trading_mode

    # ------------------------------------------------------------- ledger
    def ledger(self) -> Ledger:
        today = today_kst()
        try:
            raw = json.loads(self._ledger_file.read_text())
            if raw.get("date") == today:
                return Ledger(today, int(raw["orders"]), Decimal(raw["usd"]), Decimal(raw["krw"]))
        except (OSError, ValueError, KeyError):
            pass
        return Ledger(today)

    def _save_ledger(self, led: Ledger) -> None:
        tmp = self._ledger_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(led.to_json()))
        os.replace(tmp, self._ledger_file)

    def cap_problems(self, currency: str, notional: Decimal, side: str) -> list[str]:
        """주문 1건 + 오늘 누적 한도 검사. 문제 없으면 빈 리스트.

        매도는 위험을 줄이는 쪽이라 (손절이 한도에 막히면 안 됨) 금액·건수 한도에서 뺀다.
        매도 수량은 별도로 매도가능수량 검사를 거친다.
        """
        problems: list[str] = []
        if side == "SELL":
            return problems
        led = self.ledger()
        c = self.s
        if c.daily_max_orders is not None and led.orders + 1 > c.daily_max_orders:
            problems.append(f"오늘 매수 주문 횟수 한도 초과 ({led.orders}/{c.daily_max_orders}건)")
        if currency == "USD":
            per, daily, used, fmt = c.max_order_usd, c.daily_max_usd, led.usd, (lambda v: f"${v:,.2f}")
        else:
            per, daily, used, fmt = c.max_order_krw, c.daily_max_krw, led.krw, (lambda v: f"{v:,.0f}원")
        if per is not None and notional > per:
            problems.append(f"1회 매수 한도 초과: 약 {fmt(notional)} > {fmt(per)}")
        if daily is not None and used + notional > daily:
            problems.append(f"오늘 누적 매수 한도 초과: {fmt(used + notional)} > {fmt(daily)}")
        return problems

    def record(self, currency: str, notional: Decimal, side: str) -> None:
        if side == "SELL":
            return  # 매도는 한도 장부에 넣지 않음 (cap_problems 참고)
        led = self.ledger()
        led.orders += 1
        if currency == "USD":
            led.usd += notional
        else:
            led.krw += notional
        self._save_ledger(led)

    # ------------------------------------------------------------ previews
    def new_preview(self, body: dict[str, Any], currency: str, notional: Decimal, summary: str) -> Preview:
        self._gc()
        pid = "p" + secrets.token_hex(8)  # 17자: clientOrderId(최대 36자, 영숫자) 로 그대로 사용
        code = f"{secrets.randbelow(1_000_000):06d}" if self.s.ntfy_topic else None
        p = Preview(pid, time.time(), body, currency, notional, summary, code)
        self.previews[pid] = p
        return p

    def _gc(self) -> None:
        now = time.time()
        for k in [k for k, v in self.previews.items() if now - v.created > self.s.preview_ttl_sec * 4]:
            self.previews.pop(k, None)

    # -------------------------------------------------------------- notify
    async def push_code(self, p: Preview) -> bool:
        if not (self.s.ntfy_topic and p.confirm_code):
            return False
        # JSON 발행 방식: 헤더에 한글을 못 넣는 문제를 피한다 (https://docs.ntfy.sh/publish/#publish-as-json)
        payload = {
            "topic": self.s.ntfy_topic,
            "title": "토스 주문 확인코드",
            "message": f"{p.summary}\n\n확인코드: {p.confirm_code}\n({self.s.preview_ttl_sec // 60}분 안에 AI 채팅에 입력)",
            "priority": 5,
            "tags": ["warning"],
        }
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.post(f"{self.s.ntfy_server}/", json=payload)
                return r.status_code < 300
        except Exception:  # noqa: BLE001 - 알림 실패는 주문 실행 불가로만 이어지고, 서버를 죽이면 안 됨
            return False

    # ----------------------------------------------------------------- log
    def log(self, event: str, **data: Any) -> None:
        rec = {"ts": datetime.now(KST).isoformat(timespec="seconds"), "event": event, "mode": self.s.trading_mode}
        rec.update({k: (str(v) if isinstance(v, Decimal) else v) for k, v in data.items()})
        with open(self._log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def recent_log(self, n: int = 20) -> list[dict[str, Any]]:
        try:
            lines = self._log_file.read_text(encoding="utf-8").splitlines()[-n:]
            return [json.loads(x) for x in lines]
        except (FileNotFoundError, ValueError):
            return []
