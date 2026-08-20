import asyncio, os

from weakref import WeakKeyDictionary

from dotenv import load_dotenv
from aiolimiter import AsyncLimiter

from .token_bucket import LimitPolicy, LimitRegistry, NoopUmbrella, AdaptiveUmbrella

load_dotenv()


# 未登錄於 MODEL_LIMITS 或 agent YAML 的模型套用的策略。
# 刻意不預設為 enforced —— 未登錄的模型沒有配額數值可管制，那只會產生
# 「用某組預設配額管制」的隱性行為，比明確報錯更難除錯。
DEFAULT_POLICY = LimitPolicy(os.getenv("LIMIT_DEFAULT_POLICY", LimitPolicy.CONCURRENCY_ONLY.value))


MODEL_LIMITS = {
    ### OPENAI Tier 1 Rate limit at 2025/09
    "gpt-4.1": {"TPM": 30000, "RPM": 500, "TPD": 90000},
    "gpt-4.1-mini": {"TPM": 200000, "RPM": 500, "TPD": 2000000},
    "gpt-4o": {"TPM": 30000, "RPM": 500, "TPD": 90000},
    "gpt-4o-mini": {"TPM": 200000, "RPM": 500, "TPD": 2000000},
    "text-embedding-3-large": {"TPM": 1000000, "RPM": 3000, "TPD": 3000000},
    "text-embedding-3-small": {"TPM": 1000000, "RPM": 3000, "TPD": 3000000},
    "text-ada-embedding-ada-002": {"TPM": 1000000, "RPM": 3000, "TPD": 3000000},

    ### Gemini API Free Tier Rate limit at 2026/04
    "gemma-4-31b-it": {"TPM": 30000, "RPM": 15, "RPD": 1500},

    ### 本地 Ollama：本地推理無帳單，TPM/RPM 不對應任何真實約束，
    ### 唯一有意義的管制是併發數（GPU 序列化執行）。
    "glm-ocr-optimized:latest": {"policy": "concurrency_only"},
}

GLOBAL_CONCURRENCY = int(os.getenv("GLOBAL_CONCURRENCY", 6))
GLOBAL_RPM = int(os.getenv("RPM", 200))
GLOBAL_TPM = int(os.getenv("TPM", 30000))

global_sem = asyncio.Semaphore(GLOBAL_CONCURRENCY)

# 保留供已棄用的 with_global_limits 使用；新程式碼一律走 get_global_rpm_limiter()。
global_rpm_limiter = AsyncLimiter(GLOBAL_RPM, time_period=60)

_rpm_limiter_by_loop: "WeakKeyDictionary[asyncio.AbstractEventLoop, AsyncLimiter]" = WeakKeyDictionary()


def get_global_rpm_limiter() -> AsyncLimiter:
    """取得當前 event loop 專屬的全域 RPM 限制器。

    AsyncLimiter 跨 event loop 重用會產生未定義行為（aiolimiter 會發出 RuntimeWarning）。
    本模組是被下游專案引用的通用模組，無法假設呼叫端只有一個 loop —— 測試套件常
    為每個案例建立新 loop，重複呼叫 asyncio.run() 也會。因此依 loop 分別建立實例。

    以 WeakKeyDictionary 保存，loop 被回收時對應的限制器一併釋放。
    """
    loop = asyncio.get_running_loop()
    limiter = _rpm_limiter_by_loop.get(loop)
    if limiter is None:
        limiter = AsyncLimiter(GLOBAL_RPM, time_period=60)
        _rpm_limiter_by_loop[loop] = limiter
    return limiter

registry = LimitRegistry(MODEL_LIMITS, default_policy=DEFAULT_POLICY)

umbrella = NoopUmbrella()
# 可選：啟用跨模型的全域 TPM 上限。只加總 enforced 模型的 TPM ——
# concurrency_only / unlimited 的 entry 沒有 TPM 鍵。
# umbrella = AdaptiveUmbrella(
#     init_tpm=sum(v["TPM"] for v in MODEL_LIMITS.values() if "TPM" in v)
# )
