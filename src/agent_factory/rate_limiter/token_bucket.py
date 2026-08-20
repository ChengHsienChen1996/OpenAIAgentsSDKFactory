import asyncio
import enum
import logging
import time

from typing import Any, Dict, Optional
from aiolimiter import AsyncLimiter

logger = logging.getLogger("rate.guard")


class LimitPolicy(str, enum.Enum):
    """單一模型的速率限制策略。

    差異由 registry 回傳的限制器型別吸收，呼叫端不得依 policy 分支
    （見 docs/architecture.md 原則 2）。
    """

    ENFORCED = "enforced"                  # 實際管制 TPM / RPM / RPD，適用雲端供應商
    CONCURRENCY_ONLY = "concurrency_only"  # 只受全域併發約束，適用本地／自架模型
    UNLIMITED = "unlimited"                # 完全不管制，特殊情境


class AsyncTokenBucket:
    def __init__(self, capacity: int, refill_rate_per_sec: float):
        self.capacity = capacity
        self.tokens = float(capacity)
        self.refill = float(refill_rate_per_sec)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, amount: int):
        # 單次請求要求的量超過桶容量時，無論等多久都不可能滿足 —— 舊版會在此無限迴圈，
        # 表現為程式靜默卡死。明確報錯才能讓呼叫端知道是估算或設定有問題。
        if amount > self.capacity:
            raise ValueError(
                f"單次請求預扣量 {amount} 超過該模型 TPM 上限 {self.capacity}，"
                f"無論等待多久都無法滿足。請檢查輸入估算是否異常，或調高該模型的 TPM 設定。"
            )

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


class NullTokenBucket:
    """介面與 AsyncTokenBucket 相同的 no-op 實作，所有操作立即返回。

    供 concurrency_only / unlimited 策略使用：本地推理無帳單，TPM 不對應任何真實約束。
    退款到此桶同樣是 no-op，不會出錯。
    """

    capacity = float("inf")
    tokens = float("inf")

    async def acquire(self, amount: int) -> None:
        return

    async def refund(self, amount: int) -> None:
        return


class NullLimiter:
    """介面與 aiolimiter.AsyncLimiter 相同的 async context manager，立即通過。"""

    async def __aenter__(self) -> "NullLimiter":
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None


_NULL_BUCKET = NullTokenBucket()
_NULL_LIMITER = NullLimiter()

# 限制設定的來源，數字越大優先序越高。
SOURCE_MODEL_LIMITS = "model_limits"
SOURCE_YAML = "yaml"

_SOURCE_PRIORITY = {SOURCE_MODEL_LIMITS: 0, SOURCE_YAML: 1}


class LimitRegistry:
    """依模型的 policy 建立對應限制器。

    ``bucket()`` / ``rpm()`` / ``rpd()`` 三個方法是跨專案契約，簽名不得變更。
    policy 差異完全由回傳的物件型別表達，呼叫端不需要（也不應該）知道 policy 是什麼。
    """

    def __init__(
        self,
        model_limits: Dict[str, Dict[str, Any]],
        default_policy: LimitPolicy = LimitPolicy.CONCURRENCY_ONLY,
    ):
        # model_limits 例如：
        #   {"gpt-4o": {"TPM": 30000, "RPM": 60},                 # 未寫 policy 視為 enforced
        #    "glm-ocr-optimized:latest": {"policy": "concurrency_only"}}
        self.model_buckets: Dict[str, Any] = {}
        self.model_rpms: Dict[str, Any] = {}
        self.model_rpds: Dict[str, AsyncLimiter] = {}
        self.model_policies: Dict[str, LimitPolicy] = {}
        self.default_policy = LimitPolicy(default_policy)

        # 記錄已經警告過的模型名，讓每個未登錄模型只洗一次版面。
        self._warned_models: set = set()

        # model -> (來源, 正規化後的設定)，供衝突判定與冪等檢查使用。
        self._registered: Dict[str, tuple] = {}

        for model, cfg in model_limits.items():
            self.register(model, cfg)

    def register(self, model: str, cfg: Dict[str, Any], *, source: str = SOURCE_MODEL_LIMITS) -> None:
        """登錄單一模型的限制設定。

        registry 為模組層全域單例，同一模型可能被多次註冊（多個 agent、多個工廠實例、
        重複初始化）。衝突處理規則：

        - **設定相同**：直接返回，重複註冊為冪等操作，不報錯也不重建限制器。
          重建會把桶內既有餘額歸零，等同於靜默清空配額。
        - **設定不同、來源優先序較高**（YAML > MODEL_LIMITS）：覆寫並 log info。
        - **設定不同、來源優先序相同或較低**：保留先前設定並 log warning，不做合併 ——
          合併兩組不同的配額會產生沒有人宣告過的隱性行為。

        Args:
            model: 模型名稱。
            cfg: 限制設定。未指定 ``policy`` 時視為 ``enforced``（維持既有 MODEL_LIMITS 的語意）。
            source: 設定來源，決定衝突時誰勝出。
        """
        normalized = self._normalize_cfg(cfg)
        previous = self._registered.get(model)

        if previous is not None:
            previous_source, previous_cfg = previous

            if previous_cfg == normalized:
                return  # 冪等：設定完全相同，不重建限制器

            if _SOURCE_PRIORITY.get(source, 0) > _SOURCE_PRIORITY.get(previous_source, 0):
                logger.info(
                    "limits_overridden model=%s from=%s to=%s",
                    model, previous_source, source,
                )
            else:
                logger.warning(
                    "limits_conflict model=%s existing_source=%s ignored_source=%s："
                    "同一模型被宣告了兩組不同的限制設定，以先註冊的為準，不做合併",
                    model, previous_source, source,
                )
                return

        self._registered[model] = (source, normalized)
        policy = LimitPolicy(cfg.get("policy", LimitPolicy.ENFORCED))
        self.model_policies[model] = policy

        if policy is not LimitPolicy.ENFORCED:
            # concurrency_only 與 unlimited 都不需要 TPM/RPM 數值。
            self.model_buckets[model] = _NULL_BUCKET
            self.model_rpms[model] = _NULL_LIMITER
            return

        tpm = int(cfg["TPM"])
        rpm = int(cfg["RPM"])
        self.model_buckets[model] = AsyncTokenBucket(tpm, tpm / 60.0)
        self.model_rpms[model] = AsyncLimiter(rpm, time_period=60)

        if "RPD" in cfg and cfg["RPD"] is not None:  # 可選，有才建立
            self.model_rpds[model] = AsyncLimiter(int(cfg["RPD"]), time_period=86400)

    @staticmethod
    def _normalize_cfg(cfg: Dict[str, Any]) -> tuple:
        """把設定 dict 正規化為可比較的形式，供冪等判定使用。

        鍵順序不同、policy 寫明或省略、RPD 為 None 或缺席，都應視為同一組設定。
        """
        policy = LimitPolicy(cfg.get("policy", LimitPolicy.ENFORCED)).value
        quotas = tuple(
            (key, int(cfg[key]))
            for key in ("TPM", "RPM", "RPD")
            if cfg.get(key) is not None
        )
        return (policy, quotas)

    def policy(self, model: str) -> LimitPolicy:
        """取得該模型實際套用的策略（未登錄時為 default_policy）。"""
        return self.model_policies.get(model, self.default_policy)

    def _warn_unregistered_once(self, model: str) -> None:
        if model in self._warned_models:
            return
        self._warned_models.add(model)

        if self.default_policy is LimitPolicy.ENFORCED:
            # 未登錄的模型沒有 TPM/RPM 數值可管制，enforced 無從執行。
            logger.error(
                "model_not_registered model=%s default_policy=enforced："
                "未登錄的模型沒有配額數值可管制，實際將不受限制執行。"
                "請在 MODEL_LIMITS 或 agent YAML 的 model_params.limits 補上設定",
                model,
            )
            return

        logger.warning(
            "model_not_registered model=%s default_policy=%s："
            "該模型未登錄於 MODEL_LIMITS 或 agent YAML，將依預設策略執行",
            model, self.default_policy.value,
        )

    def bucket(self, model: str):
        try:
            return self.model_buckets[model]
        except KeyError:
            self._warn_unregistered_once(model)
            # 未登錄模型沒有配額數值，唯一可行的回傳就是 no-op 桶。
            return _NULL_BUCKET

    def rpm(self, model: str):
        try:
            return self.model_rpms[model]
        except KeyError:
            self._warn_unregistered_once(model)
            return _NULL_LIMITER

    def rpd(self, model: str) -> Optional[AsyncLimiter]:
        # 未設定 RPD 的模型回傳 None，呼叫端既有的 `if rpd_limiter:` 分支據此略過。
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
        # umbrella 是跨模型的自適應總量上限，不是單一模型的硬性配額。
        # on_global_rl_error() 會把 capacity 乘以 dec_mult 縮減（下限 min_tpm），
        # 因此單筆預扣量可能在縮減後超過當前 capacity。此時不應讓請求失敗 ——
        # 那會把「全域節流」變成「單筆請求被拒」。改為取用當前可得的全部額度並示警。
        bucket = self.bucket
        if tokens > bucket.capacity:
            logger.warning(
                "umbrella_capacity_exceeded requested=%d capacity=%d："
                "全域 umbrella 已縮減至低於本次預扣量，改為取用全部可得額度",
                tokens, bucket.capacity,
            )
            await bucket.acquire(bucket.capacity)
            return

        await bucket.acquire(tokens)

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