import asyncio, os, warnings

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

# ---------------------------------------------------------------------------
# 全域同步原語
#
# asyncio 的同步原語會在「首次需要等待」時綁定當前 event loop，之後於其他 loop
# 使用即為未定義行為。模組層單例被跨 loop 共用時：
#
#   - asyncio.Semaphore：再次發生競爭時直接拋 RuntimeError（請求失敗）
#   - aiolimiter.AsyncLimiter：發出 warning 後自行重綁，節流仍正確生效
#
# 因此**只有 Semaphore 需要依 loop 建立**。詳見 .agent/notes/event-loop-binding.md。
#
# 為什麼 RPM/RPD 的模型層限制器維持共用：那些是供應商的配額，依 loop 分別建立會讓
# 每個 loop 各拿一份配額，實際請求量變成 N 倍而撞 429 —— 對配額型限制器而言，
# 依 loop 分割是比警告噪音更糟的錯誤。併發上限則不同，它是本機資源保護而非供應商配額，
# 且真正的配額仍由共用的 token bucket 與 RPM 限制器把關，因此依 loop 分割是可接受的。
# ---------------------------------------------------------------------------

_legacy_global_sem = asyncio.Semaphore(GLOBAL_CONCURRENCY)
_legacy_global_rpm_limiter = AsyncLimiter(GLOBAL_RPM, time_period=60)

_sem_by_loop: "WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = WeakKeyDictionary()
_rpm_limiter_by_loop: "WeakKeyDictionary[asyncio.AbstractEventLoop, AsyncLimiter]" = WeakKeyDictionary()

_DEPRECATED_GLOBALS = {
    "global_sem": ("_legacy_global_sem", "get_global_sem"),
    "global_rpm_limiter": ("_legacy_global_rpm_limiter", "get_global_rpm_limiter"),
}


def __getattr__(name: str):
    """讓舊的模組層符號仍可取用，但發出 DeprecationWarning。

    ``global_sem`` 與 ``global_rpm_limiter`` 是模組的公開符號，下游專案可能直接引用，
    直接移除屬破壞性變更（見 docs/architecture.md 原則 1）。改以 PEP 562 的
    module ``__getattr__`` 攔截，取用時提示改用對應的 per-loop 取得函式。
    """
    entry = _DEPRECATED_GLOBALS.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    attribute, replacement = entry
    warnings.warn(
        f"{name} 已棄用：模組層單例跨 event loop 使用會產生未定義行為，"
        f"請改用 {replacement}()。本符號將於下一個主要版本移除。",
        DeprecationWarning,
        stacklevel=2,
    )
    return globals()[attribute]


def _drop_closed_loops(mapping: WeakKeyDictionary) -> None:
    """清掉已關閉 loop 的項目。

    WeakKeyDictionary 單獨並不足夠：``asyncio.Semaphore`` 在發生競爭後會把
    ``_loop`` 指回該 loop，``AsyncLimiter`` 首次使用後也會保存 ``_event_loop``——
    也就是 **value 強參照了 key**，弱參照因而永遠不會被觸發。

    實測：連續 5 次 ``asyncio.run()`` 且每次都製造競爭，gc 三輪後 5 個項目全數殘留；
    不製造競爭則為 0。也就是說，洩漏正好發生在本機制要處理的情境上。

    因此改以「loop 是否已關閉」主動清理。項目數等同於同時存活的 loop 數，通常為 1。
    """
    for loop in [loop for loop in mapping if loop.is_closed()]:
        del mapping[loop]


def get_global_sem() -> asyncio.Semaphore:
    """取得當前 event loop 專屬的全域併發 semaphore。

    ``asyncio.Semaphore`` 在發生競爭（同時請求數超過 GLOBAL_CONCURRENCY）時會綁定
    當時的 event loop；若另一個 loop 再次發生競爭，會拋出
    ``RuntimeError: ... is bound to a different event loop``。

    這是一個間歇性、負載相關的故障 —— 低負載時完全正常，兩個 loop 都跑到超過併發上限
    才會爆，且錯誤訊息無法指向根因。因此依 loop 分別建立實例。

    語意影響：多 loop 情境下併發上限變成「每個 loop 各 GLOBAL_CONCURRENCY 個」。
    單一 loop（絕大多數正式環境）行為完全不變；而多 loop 情境在此修正前是直接拋錯，
    本就沒有可保障的語意。

    以 WeakKeyDictionary 保存，並在取用時清掉已關閉 loop 的項目（見 _drop_closed_loops）。
    """
    loop = asyncio.get_running_loop()
    _drop_closed_loops(_sem_by_loop)
    sem = _sem_by_loop.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(GLOBAL_CONCURRENCY)
        _sem_by_loop[loop] = sem
    return sem


def get_global_rpm_limiter() -> AsyncLimiter:
    """取得當前 event loop 專屬的全域 RPM 限制器。

    AsyncLimiter 跨 event loop 重用會產生未定義行為（aiolimiter 會發出 RuntimeWarning）。
    本模組是被下游專案引用的通用模組，無法假設呼叫端只有一個 loop —— 測試套件常
    為每個案例建立新 loop，重複呼叫 asyncio.run() 也會。因此依 loop 分別建立實例。

    以 WeakKeyDictionary 保存，並在取用時清掉已關閉 loop 的項目（見 _drop_closed_loops）。

    註：模型層的 RPM/RPD 限制器刻意維持共用，理由見本節上方註解。
    """
    loop = asyncio.get_running_loop()
    _drop_closed_loops(_rpm_limiter_by_loop)
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
