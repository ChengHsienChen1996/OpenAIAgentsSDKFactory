import asyncio
import time

from typing import Dict
from aiolimiter import AsyncLimiter


class AsyncTokenBucket:
    def __init__(self, capacity: int, refill_rate_per_sec: float):
        self.capacity = capacity
        self.tokens = float(capacity)
        self.refill = float(refill_rate_per_sec)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, amount: int):
        while True:
            async with self._lock:
                now = time.monotonic()
                dt = now - self._last
                if dt > 0.0:
                    self.tokens = min(self.capacity, self.tokens + dt*self.refill)
                    self._last = now
                if self.tokens >= amount:
                    self.tokens -= amount
                    return
                deficit = amount - self.tokens
                wait = max(0.05, min(deficit/self.refill if self.refill>0 else 1.0, 2.0))
            await asyncio.sleep(wait)
    async def refund(self, amount: int):
        # 不可寫成 int(self.tokens) + amount：那會在每次退款時無條件捨去小數部分，
        # 讓桶內餘額隨呼叫次數持續流失（每次最多 1 token，長時間執行會累積成可觀的缺額）。
        async with self._lock:
            self.tokens = min(self.capacity, self.tokens + amount)


class LimitRegistry:
    def __init__(self, model_limits: Dict[str, Dict[str, int]]):
        # model_limits 例如：{"gpt-4o": {"TPM": 30000, "RPM": 60}, ...}
        self.model_buckets: Dict[str, AsyncTokenBucket] = {}
        self.model_rpms: Dict[str, AsyncLimiter] = {}
        self.model_rpds: Dict[str, AsyncLimiter] = {}

        for m, cfg in model_limits.items():
            tpm = int(cfg["TPM"])
            rpm = int(cfg["RPM"])
            self.model_buckets[m] = AsyncTokenBucket(tpm, tpm / 60.0)
            self.model_rpms[m] = AsyncLimiter(rpm, time_period=60)

            if "RPD" in cfg:  # 可選，有才建立
                self.model_rpds[m] = AsyncLimiter(cfg["RPD"], time_period=86400)

    def bucket(self, model: str) -> AsyncTokenBucket:
        return self.model_buckets[model]

    def rpm(self, model: str) -> AsyncLimiter:
        return self.model_rpms[model]

    def rpd(self, model: str):
        return self.model_rpds.get(model)


# ========= 可選 umbrella（預設用 no-op；需要時替換實作） =========
class NoopUmbrella:
    async def acquire(self, tokens: int): return
    async def refund(self, tokens: int): return
    async def on_ok_tick(self): pass
    async def on_global_rl_error(self): pass


# （可選）自適應 umbrella；不需要可略過，將 umbrella 設為 NoopUmbrella()
class AdaptiveUmbrella:
    def __init__(self, init_tpm: int, min_tpm: int = 5_000, max_tpm: int = 10**9,
                 inc_per_min: int = 1_000, dec_mult: float = 0.75):
        self.capacity = init_tpm
        self.min_tpm, self.max_tpm = min_tpm, max_tpm
        self.inc_per_min, self.dec_mult = inc_per_min, dec_mult
        self._rebuild_bucket()

        self._lock = asyncio.Lock()
        self._last_inc = time.monotonic()

    def _rebuild_bucket(self):
        self.bucket = AsyncTokenBucket(self.capacity, self.capacity / 60.0)

    async def acquire(self, tokens: int):
        await self.bucket.acquire(tokens)

    async def refund(self, tokens: int):
        await self.bucket.refund(tokens)

    async def on_ok_tick(self):
        async with self._lock:
            now = time.monotonic()
            if now - self._last_inc >= 60.0:
                self.capacity = min(self.max_tpm, self.capacity + self.inc_per_min)
                self._rebuild_bucket()
                self._last_inc = now

    async def on_global_rl_error(self):
        async with self._lock:
            self.capacity = max(self.min_tpm, int(self.capacity * self.dec_mult))
            self._rebuild_bucket()
            self._last_inc = time.monotonic()