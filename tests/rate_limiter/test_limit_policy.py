"""限制策略與 Null 限制器的測試。

核心約束：policy 差異必須由 registry 回傳的物件型別吸收，
``wrappers.py`` 內不得出現任何 policy 分支（見 docs/architecture.md 原則 2）。

所有取得配額的案例都設了 timeout：AsyncTokenBucket 的失敗模式是無限等待而非拋錯。
"""
import asyncio
import inspect
import logging

import pytest

from aiolimiter import AsyncLimiter
from agents import Agent, ModelSettings

from agent_factory.rate_limiter import limits_parameters
from agent_factory.rate_limiter.token_bucket import (
    AdaptiveUmbrella,
    AsyncTokenBucket,
    LimitPolicy,
    LimitRegistry,
    NoopUmbrella,
    NullLimiter,
    NullTokenBucket,
)
from agent_factory.rate_limiter.wrappers import limits_guard_multi

ENFORCED_MODEL = "gpt-4o"
LOCAL_MODEL = "glm-ocr-optimized:latest"
UNKNOWN_MODEL = "some-model-nobody-registered"


@pytest.fixture
def registry() -> LimitRegistry:
    return LimitRegistry(
        {
            ENFORCED_MODEL: {"TPM": 30000, "RPM": 500},
            "gemma-4-31b-it": {"TPM": 30000, "RPM": 15, "RPD": 1500},
            LOCAL_MODEL: {"policy": "concurrency_only"},
            "special-model": {"policy": "unlimited"},
        }
    )


# --------------------------------------------------------------------------- #
# Null 實作的介面等價性
# --------------------------------------------------------------------------- #

def test_null_bucket_interface_matches_real_bucket():
    """NullTokenBucket 必須有 AsyncTokenBucket 的全部公開方法，且簽名相容。"""
    for name in ("acquire", "refund"):
        real = getattr(AsyncTokenBucket, name)
        null = getattr(NullTokenBucket, name)
        assert inspect.iscoroutinefunction(null)
        assert list(inspect.signature(null).parameters) == list(inspect.signature(real).parameters)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_null_bucket_operations_are_noop():
    """Null 桶的取用與退款都立即返回，且不限量。"""
    bucket = NullTokenBucket()

    await bucket.acquire(10**12)
    await bucket.refund(10**12)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_null_limiter_is_async_context_manager():
    """NullLimiter 的用法必須與 AsyncLimiter 完全一致（async with）。"""
    limiter = NullLimiter()

    async with limiter:
        pass

    # 與真實 AsyncLimiter 相同，可重複進入
    async with limiter:
        pass


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_refund_to_null_bucket_does_not_error():
    """Phase 2 修好的退款路徑走到 Null 桶時不應出錯。"""
    reg = LimitRegistry({LOCAL_MODEL: {"policy": "concurrency_only"}})

    await reg.bucket(LOCAL_MODEL).acquire(999999)
    await reg.bucket(LOCAL_MODEL).refund(999999)


# --------------------------------------------------------------------------- #
# registry 依 policy 回傳對應型別
# --------------------------------------------------------------------------- #

def test_enforced_policy_returns_real_limiters(registry):
    """未指定 policy 的既有 entry 視為 enforced，回傳真實限制器。"""
    assert isinstance(registry.bucket(ENFORCED_MODEL), AsyncTokenBucket)
    assert isinstance(registry.rpm(ENFORCED_MODEL), AsyncLimiter)
    assert registry.policy(ENFORCED_MODEL) is LimitPolicy.ENFORCED


@pytest.mark.parametrize("model", [LOCAL_MODEL, "special-model"])
def test_non_enforced_policy_returns_null_limiters(registry, model):
    """concurrency_only 與 unlimited 都回傳 no-op 限制器，且不需要 TPM/RPM 數值。"""
    assert isinstance(registry.bucket(model), NullTokenBucket)
    assert isinstance(registry.rpm(model), NullLimiter)


def test_rpd_returns_none_when_not_configured(registry):
    """未設定 RPD 的模型回傳 None —— wrappers 既有的 `if rpd_limiter:` 分支據此略過。"""
    assert registry.rpd(ENFORCED_MODEL) is None
    assert registry.rpd(LOCAL_MODEL) is None
    assert registry.rpd(UNKNOWN_MODEL) is None


def test_rpd_returns_limiter_when_configured(registry):
    """有設定 RPD 時仍回傳真實限制器。"""
    assert isinstance(registry.rpd("gemma-4-31b-it"), AsyncLimiter)


# --------------------------------------------------------------------------- #
# 未登錄模型
# --------------------------------------------------------------------------- #

def test_unregistered_model_no_longer_raises_key_error(registry):
    """未登錄模型不再 KeyError，改依 DEFAULT_POLICY 運作。"""
    assert isinstance(registry.bucket(UNKNOWN_MODEL), NullTokenBucket)
    assert isinstance(registry.rpm(UNKNOWN_MODEL), NullLimiter)


def test_unregistered_model_warns_only_once(registry, caplog):
    """每個未登錄模型只警告一次，避免洗版。"""
    with caplog.at_level(logging.WARNING, logger="rate.guard"):
        registry.bucket(UNKNOWN_MODEL)
        registry.bucket(UNKNOWN_MODEL)
        registry.rpm(UNKNOWN_MODEL)

    assert caplog.text.count("model_not_registered") == 1
    assert UNKNOWN_MODEL in caplog.text


def test_unregistered_models_warn_independently(registry, caplog):
    """不同的未登錄模型各自警告一次。"""
    with caplog.at_level(logging.WARNING, logger="rate.guard"):
        registry.bucket("model-a")
        registry.bucket("model-b")

    assert caplog.text.count("model_not_registered") == 2


def test_default_policy_enforced_logs_error(caplog):
    """DEFAULT_POLICY 設為 enforced 時，未登錄模型無配額可管制，應以 error 標記。"""
    reg = LimitRegistry({}, default_policy=LimitPolicy.ENFORCED)

    with caplog.at_level(logging.ERROR, logger="rate.guard"):
        reg.bucket(UNKNOWN_MODEL)

    assert "default_policy=enforced" in caplog.text


def test_module_default_policy_is_not_enforced():
    """預設策略不得為 enforced —— 那會產生「用某組預設配額管制」的隱性行為。"""
    assert limits_parameters.DEFAULT_POLICY is not LimitPolicy.ENFORCED


# --------------------------------------------------------------------------- #
# 無限等待的修正
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_acquire_above_capacity_raises_instead_of_hanging():
    """預扣量超過桶容量時立即拋 ValueError，而非無限等待。

    timeout 是本測試的重點：舊版會在此卡死，表現為 CI 掛住而非測試失敗。
    """
    bucket = AsyncTokenBucket(capacity=1000, refill_rate_per_sec=1000)

    with pytest.raises(ValueError, match="超過該模型 TPM 上限"):
        await bucket.acquire(1001)


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_acquire_exactly_capacity_still_works():
    """剛好等於容量時仍應正常取得，不可誤判為超量。"""
    bucket = AsyncTokenBucket(capacity=1000, refill_rate_per_sec=1000)

    await bucket.acquire(1000)

    assert bucket.tokens == pytest.approx(0, abs=1)


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_null_bucket_has_no_capacity_ceiling():
    """Null 桶不受容量限制，超大預扣量也立即通過。"""
    await NullTokenBucket().acquire(10**15)


# --------------------------------------------------------------------------- #
# AdaptiveUmbrella 與新 ValueError 的交互作用
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_adaptive_umbrella_survives_capacity_shrink(caplog):
    """umbrella 縮減 capacity 後，單筆超量的預扣不應讓請求失敗。

    umbrella 是跨模型的自適應總量上限，不是單一模型的硬性配額；
    若在此拋 ValueError，全域節流會變成單筆請求被拒。
    """
    umbrella = AdaptiveUmbrella(init_tpm=10_000, min_tpm=5_000)

    # 觸發縮減：10000 → 7500 → 5625 → 5000（下限）
    for _ in range(5):
        await umbrella.on_global_rl_error()

    assert umbrella.capacity < 8345  # Phase 2 實測的單筆預扣量

    with caplog.at_level(logging.WARNING, logger="rate.guard"):
        await umbrella.acquire(8345)

    assert "umbrella_capacity_exceeded" in caplog.text


# --------------------------------------------------------------------------- #
# 架構約束：wrappers 不得有 policy 分支
# --------------------------------------------------------------------------- #

def test_wrappers_contains_no_policy_branch():
    """wrappers.py 不得依 policy 分支 —— 差異全由 registry 回傳的型別吸收。

    以原始碼檢查取代人工 diff 複查，避免日後改動時無意間破壞此約束。
    """
    from agent_factory.rate_limiter import wrappers

    source = inspect.getsource(wrappers)

    assert "LimitPolicy" not in source
    assert "concurrency_only" not in source
    assert "unlimited" not in source
    assert ".policy(" not in source


# --------------------------------------------------------------------------- #
# 端到端：concurrency_only 模型走完整個等待鏈
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_concurrency_only_model_waits_are_zero(mock_run_result):
    """本地模型走完等待鏈時，各限制階段的 wait_s 皆應為 0。"""
    events = []
    reg = LimitRegistry({LOCAL_MODEL: {"policy": "concurrency_only"}})

    def _trace(event, fields):
        events.append((event, fields))

    class _Runner:
        def __init__(self, agent):
            self.agent = agent

        @limits_guard_multi(
            registry=reg,
            umbrella=NoopUmbrella(),
            input_arg="input_",
            trace=_trace,
            warn_after_s=999.0,
            heartbeat_every_s=0,
        )
        async def run(self, input_=None, context=None):
            return mock_run_result([(LOCAL_MODEL, 100)])

    agent = Agent(
        name="LocalAgent",
        instructions="test",
        model=LOCAL_MODEL,
        model_settings=ModelSettings(max_tokens=None),
    )

    await _Runner(agent).run(input_="hi")

    waits = {
        f["stage"]: f["wait_s"]
        for name, f in events
        if name == "acquired" and f["stage"] in ("umbrella_tpm", f"model_tpm:{LOCAL_MODEL}", "model_rpm_chain")
    }

    assert len(waits) == 3
    assert all(wait == 0 for wait in waits.values()), waits


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_local_model_no_longer_needs_fake_tpm():
    """limits_parameters 的本地模型設定不得再出現假的 TPM 魔數。"""
    cfg = limits_parameters.MODEL_LIMITS[LOCAL_MODEL]

    assert cfg == {"policy": "concurrency_only"}
    assert "TPM" not in cfg
