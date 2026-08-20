"""預扣退款校正的測試。

退款失效不會立即可見 —— 表現為配額緩慢流失，直到某次呼叫毫無理由地卡住。
因此除了單元層的比對邏輯，這裡也有「連續多次呼叫後桶內餘額不單調下降」的回歸測試
（見 docs/architecture.md 測試要點 4）。

所有取得配額的案例都設了 timeout：AsyncTokenBucket 的失敗模式是無限等待而非拋錯。
"""
import logging

import pytest

from agents import Agent, ModelSettings

from agent_factory.rate_limiter.token_bucket import AsyncTokenBucket, LimitRegistry, NoopUmbrella
from agent_factory.rate_limiter.wrappers import (
    is_version_variant,
    limits_guard_multi,
    resolve_actual_usage,
)

TEST_MODEL = "gpt-4.1"
INSTRUCTIONS = "你是一個測試用 agent。"

MAX_OUTPUT_TOKENS = 2048
OUTPUT_BUFFER_MULT = 1.2
SAFETY_PAD_TOKENS = 64
PER_ROUND_PAD = 300

TEST_TPM = 1_000_000
TEST_RPM = 10_000


# --------------------------------------------------------------------------- #
# 版本變體比對：不可誤配
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "response_model,configured,expected",
    [
        # 精確相符
        ("gpt-4.1", "gpt-4.1", True),
        # 供應商的日期版本名 → 應相符
        ("gpt-4.1-2025-04-14", "gpt-4.1", True),
        ("gpt-4o-2024-11-20", "gpt-4o", True),
        ("gpt-4o-mini-2024-07-18", "gpt-4o-mini", True),
        # 不同模型 → 絕不可相符（各有各的配額）
        ("gpt-4.1-mini", "gpt-4.1", False),
        ("gpt-4.1-nano", "gpt-4.1", False),
        ("gpt-4.1-mini-2025-04-14", "gpt-4.1", False),
        ("gpt-4o-mini", "gpt-4o", False),
        # 反向：設定 mini、回應是完整版
        ("gpt-4.1", "gpt-4.1-mini", False),
        # 空值
        ("", "gpt-4.1", False),
        ("gpt-4.1", "", False),
    ],
)
def test_is_version_variant(response_model, configured, expected):
    """版本變體比對的正確性。

    gpt-4.1 誤配到 gpt-4.1-mini 的用量，會讓兩個模型的配額互相汙染。
    """
    assert is_version_variant(response_model, configured) is expected


# --------------------------------------------------------------------------- #
# 三段式用量解析
# --------------------------------------------------------------------------- #

def test_resolve_usage_exact_match_wins():
    """第一段：模型名精確相符時直接採用。"""
    used = {"gpt-4.1": 500, "gpt-4.1-mini": 900}

    assert resolve_actual_usage("gpt-4.1", used, 1400, 2) == 500


def test_resolve_usage_falls_back_to_version_variant():
    """第二段：唯一符合的版本變體。"""
    used = {"gpt-4.1-2025-04-14": 700}

    assert resolve_actual_usage("gpt-4.1", used, 700, 1) == 700


def test_resolve_usage_does_not_borrow_from_sibling_model():
    """不得把 gpt-4.1-mini 的用量算到 gpt-4.1 頭上。

    此處刻意讓 model_count=2，使第三段（單一模型取總量）不會生效，
    以獨立驗證第二段的嚴格度。
    """
    used = {"gpt-4.1-mini": 900}

    assert resolve_actual_usage("gpt-4.1", used, 900, 2) is None


def test_resolve_usage_single_model_uses_total():
    """第三段：本次只涉及單一模型時採用總量。

    這是 openai-agents 0.3.3 的實際生效路徑 —— ModelResponse 沒有 model 欄位，
    used_by_model 恆為空。
    """
    assert resolve_actual_usage("gpt-4.1", {}, 1234, 1) == 1234


def test_resolve_usage_returns_none_when_nothing_available():
    """完全取不到用量時回傳 None，由呼叫端全額退款。"""
    assert resolve_actual_usage("gpt-4.1", {}, 0, 1) is None


def test_resolve_usage_ambiguous_variants_returns_none():
    """多個變體同時符合時視為無法判定，不猜。"""
    used = {"gpt-4.1-2025-04-14": 100, "gpt-4.1-2025-08-01": 200}

    assert resolve_actual_usage("gpt-4.1", used, 300, 2) is None


# --------------------------------------------------------------------------- #
# AsyncTokenBucket.refund 精度
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_refund_preserves_fractional_tokens():
    """退款不得捨去小數 —— 每次捨去最多 1 token，長時間執行會累積成可觀缺額。"""
    bucket = AsyncTokenBucket(capacity=1000, refill_rate_per_sec=1000)
    bucket.tokens = 10.75

    await bucket.refund(5)

    assert bucket.tokens == pytest.approx(15.75)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_refund_does_not_exceed_capacity():
    """退款後不得超過桶容量。"""
    bucket = AsyncTokenBucket(capacity=100, refill_rate_per_sec=1)
    bucket.tokens = 90.0

    await bucket.refund(50)

    assert bucket.tokens == 100


# --------------------------------------------------------------------------- #
# 端到端：預扣 → 呼叫 → 校正
# --------------------------------------------------------------------------- #

def _build_runner(result, model_name: str = TEST_MODEL):
    """建立掛了 limits_guard_multi 的最小 runner，回傳 (runner, registry)。"""
    registry = LimitRegistry({model_name: {"TPM": TEST_TPM, "RPM": TEST_RPM}})

    class _Runner:
        def __init__(self, agent):
            self.agent = agent

        @limits_guard_multi(
            registry=registry,
            umbrella=NoopUmbrella(),
            input_arg="input_",
            context_arg="context",
            max_output_tokens=MAX_OUTPUT_TOKENS,
            output_buffer_mult=OUTPUT_BUFFER_MULT,
            safety_pad_tokens=SAFETY_PAD_TOKENS,
            per_round_pad=PER_ROUND_PAD,
            trace=None,
            warn_after_s=999.0,
            heartbeat_every_s=0,
        )
        async def run(self, input_=None, context=None):
            return result

    agent = Agent(
        name="TestAgent",
        instructions=INSTRUCTIONS,
        model=model_name,
        model_settings=ModelSettings(max_tokens=None),
    )
    return _Runner(agent), registry


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_refund_happens_with_real_model_response_shape(mock_run_result):
    """真實 ModelResponse 沒有 model 欄位，退款仍必須發生。

    這是本次修復的核心迴歸：改動前 used_by_model 恆為空 → actual 恆為 0
    → 退款條件 `reserved > actual > 0` 永不成立 → 預扣一次都沒退過。
    """
    runner, registry = _build_runner(mock_run_result([(TEST_MODEL, 200)]))
    bucket = registry.bucket(TEST_MODEL)
    bucket.tokens = float(TEST_TPM)
    bucket.refill = 0.0  # 關掉自動回填，讓餘額變化完全來自預扣與退款

    await runner.run(input_="hi")

    # 只應淨扣掉實際用量 200，而非整筆 reserved
    assert bucket.tokens == pytest.approx(TEST_TPM - 200)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_bucket_balance_does_not_monotonically_decrease(mock_run_result):
    """連續多次呼叫後，桶內餘額只應反映實際用量總和，不得持續流失。"""
    calls = 20
    used_per_call = 150
    runner, registry = _build_runner(mock_run_result([(TEST_MODEL, used_per_call)]))
    bucket = registry.bucket(TEST_MODEL)
    bucket.tokens = float(TEST_TPM)
    bucket.refill = 0.0

    for _ in range(calls):
        await runner.run(input_="hi")

    assert bucket.tokens == pytest.approx(TEST_TPM - calls * used_per_call)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_multi_round_tool_usage_is_summed(mock_run_result):
    """tool 多回合會產生多筆 raw_response，用量應加總而非只取第一筆。"""
    runner, registry = _build_runner(
        mock_run_result([(TEST_MODEL, 100), (TEST_MODEL, 250), (TEST_MODEL, 90)])
    )
    bucket = registry.bucket(TEST_MODEL)
    bucket.tokens = float(TEST_TPM)
    bucket.refill = 0.0

    await runner.run(input_="hi")

    assert bucket.tokens == pytest.approx(TEST_TPM - (100 + 250 + 90))


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_no_usage_triggers_full_refund_and_error_log(mock_run_result, caplog):
    """完全取不到用量時全額退款，並以 error 標記 TPM 管制未生效。"""
    runner, registry = _build_runner(mock_run_result([]))
    bucket = registry.bucket(TEST_MODEL)
    bucket.tokens = float(TEST_TPM)
    bucket.refill = 0.0

    with caplog.at_level(logging.ERROR, logger="rate.guard"):
        await runner.run(input_="hi")

    assert bucket.tokens == pytest.approx(TEST_TPM)
    assert "usage_unavailable_full_refund" in caplog.text


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_zero_usage_triggers_full_refund(mock_run_result):
    """raw_response 存在但用量為 0（供應商未回報）同樣視為取不到用量。"""
    runner, registry = _build_runner(mock_run_result([(TEST_MODEL, 0)]))
    bucket = registry.bucket(TEST_MODEL)
    bucket.tokens = float(TEST_TPM)
    bucket.refill = 0.0

    await runner.run(input_="hi")

    assert bucket.tokens == pytest.approx(TEST_TPM)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_underestimate_tops_up(mock_run_result):
    """實際用量超過預扣時應補扣差額。"""
    runner, registry = _build_runner(mock_run_result([(TEST_MODEL, 50_000)]))
    bucket = registry.bucket(TEST_MODEL)
    bucket.tokens = float(TEST_TPM)
    bucket.refill = 0.0

    await runner.run(input_="hi")

    assert bucket.tokens == pytest.approx(TEST_TPM - 50_000)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_version_named_response_is_matched(mock_run_result):
    """未來 SDK 若補上 model 欄位且回傳日期版本名，第二段比對應生效。"""
    result = mock_run_result([("gpt-4.1-2025-04-14", 300)], include_model_name=True)
    runner, registry = _build_runner(result)
    bucket = registry.bucket(TEST_MODEL)
    bucket.tokens = float(TEST_TPM)
    bucket.refill = 0.0

    await runner.run(input_="hi")

    assert bucket.tokens == pytest.approx(TEST_TPM - 300)
