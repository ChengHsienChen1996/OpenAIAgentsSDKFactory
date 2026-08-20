"""limits_guard_multi 的輸入估算接線測試。

只驗證「估算來源與 trace 分項」這一段，不涉及等待順序與退款校正
（退款為 Task 2.4 的 test_refund.py）。

所有取得配額的案例都設了 timeout：AsyncTokenBucket 的失敗模式是無限等待而非拋錯，
沒有 timeout 的話測試失敗會表現為整個 CI 掛住（見 docs/architecture.md 測試要點 2）。
"""
import base64
import logging

import pytest

from agents import Agent, ModelSettings

from agent_factory.rate_limiter.token_bucket import LimitRegistry, NoopUmbrella
from agent_factory.rate_limiter.token_counter import (
    PER_MESSAGE_OVERHEAD,
    count_tokens,
    estimate_input_tokens,
)
from agent_factory.rate_limiter.wrappers import limits_guard_multi

TEST_MODEL = "gpt-4o"
NO_ESTIMATOR_MODEL = "glm-ocr-optimized:latest"
INSTRUCTIONS = "你是一個測試用 agent。"

# 與被測裝飾器參數保持一致，供期望值計算使用
MAX_OUTPUT_TOKENS = 2048
OUTPUT_BUFFER_MULT = 1.2
SAFETY_PAD_TOKENS = 64
PER_ROUND_PAD = 300

# 本地測試桶開得夠大，讓取得配額不會實際等待
TEST_TPM = 10_000_000
TEST_RPM = 10_000


def _build_runner(trace_events: list, result, model_name: str = TEST_MODEL, max_tokens=None):
    """建立一個掛了 limits_guard_multi 的最小 runner。"""
    registry = LimitRegistry({model_name: {"TPM": TEST_TPM, "RPM": TEST_RPM}})

    def _trace(event: str, fields: dict) -> None:
        trace_events.append((event, fields))

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
            trace=_trace,
            warn_after_s=999.0,
            heartbeat_every_s=0,
        )
        async def run(self, input_=None, context=None):
            return result

    agent = Agent(
        name="TestAgent",
        instructions=INSTRUCTIONS,
        model=model_name,
        model_settings=ModelSettings(max_tokens=max_tokens),
    )
    return _Runner(agent)


def _field(trace_events: list, event: str) -> dict:
    for name, fields in trace_events:
        if name == event:
            return fields
    raise AssertionError(f"trace 事件 {event!r} 未出現，實際事件：{[n for n, _ in trace_events]}")


def _image_input(tiny_images) -> list:
    url = "data:image/png;base64," + base64.b64encode(tiny_images["png"]).decode("utf-8")
    return [
        {"role": "user", "content": [{"type": "input_image", "detail": "auto", "image_url": url}]},
        {"role": "user", "content": "Table Recognition:"},
    ]


@pytest.fixture
def result(mock_run_result):
    """回傳一筆用量遠小於預扣量的模擬結果，避免觸發補扣路徑。"""
    return mock_run_result([(TEST_MODEL, 100)])


# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_enter_user_tok_equals_estimate_total(result, tiny_images):
    """enter 的 user_tok key 保留，值改為 estimate.total（避免破壞既有 log 解析）。"""
    events = []
    runner = _build_runner(events, result)
    model_input = _image_input(tiny_images)

    await runner.run(input_=model_input)

    expected = estimate_input_tokens(model_input, TEST_MODEL).total
    assert _field(events, "enter")["user_tok"] == expected


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_estimate_ready_has_breakdown_fields(result, tiny_images):
    """estimate_ready 必須帶四個分項欄位 —— Phase 4 量測估算誤差的唯一資料來源。"""
    events = []
    runner = _build_runner(events, result)
    model_input = _image_input(tiny_images)

    await runner.run(input_=model_input)

    fields = _field(events, "estimate_ready")
    expected = estimate_input_tokens(model_input, TEST_MODEL)

    assert fields["text_tok"] == expected.text_tokens
    assert fields["image_tok"] == expected.image_tokens
    assert fields["other_tok"] == expected.other_tokens
    assert fields["image_count"] == expected.image_count == 1


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_breakdown_fields_sum_to_user_tok(result, tiny_images):
    """分項相加必須等於 user_tok，否則 trace 的分項數字無法採信。"""
    events = []
    runner = _build_runner(events, result)

    await runner.run(input_=_image_input(tiny_images))

    fields = _field(events, "estimate_ready")
    assert fields["text_tok"] + fields["image_tok"] + fields["other_tok"] == fields["user_tok"]


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_reserved_composition_is_complete(result, tiny_images):
    """reserved 的五項組成一項都不能少。

    直接以各項獨立算出的期望值比對，任何一項在改動中被遺漏都會使此測試失敗。
    """
    events = []
    runner = _build_runner(events, result)
    model_input = _image_input(tiny_images)

    await runner.run(input_=model_input)

    fields = _field(events, "estimate_ready")
    user_tok = estimate_input_tokens(model_input, TEST_MODEL).total
    sys_tok = count_tokens(INSTRUCTIONS, TEST_MODEL)

    expected = (
        user_tok
        + sys_tok
        + SAFETY_PAD_TOKENS
        + int(OUTPUT_BUFFER_MULT * MAX_OUTPUT_TOKENS)
        + PER_ROUND_PAD
    )

    assert fields["sys_tok"] == sys_tok
    assert fields["reserved_tokens"] == expected


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_model_settings_max_tokens_overrides_default(result):
    """model_settings.max_tokens 有值時應取代裝飾器的 max_output_tokens 預設。"""
    events = []
    custom_max = 512
    runner = _build_runner(events, result, max_tokens=custom_max)

    await runner.run(input_="hi")

    fields = _field(events, "estimate_ready")
    expected = (
        estimate_input_tokens("hi", TEST_MODEL).total
        + count_tokens(INSTRUCTIONS, TEST_MODEL)
        + SAFETY_PAD_TOKENS
        + int(OUTPUT_BUFFER_MULT * custom_max)
        + PER_ROUND_PAD
    )

    assert fields["reserved_tokens"] == expected


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_image_input_no_longer_reserves_millions(result, tiny_images):
    """本 phase 的核心成果：含影像的請求預扣量降到數千級。"""
    events = []
    runner = _build_runner(events, result)

    await runner.run(input_=_image_input(tiny_images))

    fields = _field(events, "estimate_ready")
    assert fields["reserved_tokens"] < 10_000
    assert fields["image_tok"] > 0
    assert fields["text_tok"] < 100


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_warns_before_waiting_when_image_estimator_missing(result, tiny_images, caplog):
    """有影像但模型無估算器時，必須在 enter 階段就示警。"""
    events = []
    runner = _build_runner(events, result, model_name=NO_ESTIMATOR_MODEL)

    with caplog.at_level(logging.WARNING, logger="rate.guard"):
        await runner.run(input_=_image_input(tiny_images))

    assert "image_estimate_unreliable" in caplog.text
    assert NO_ESTIMATOR_MODEL in caplog.text


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_no_warning_when_estimator_exists(result, tiny_images, caplog):
    """有對應估算器時不應示警，避免正常路徑產生噪音。"""
    events = []
    runner = _build_runner(events, result)

    with caplog.at_level(logging.WARNING, logger="rate.guard"):
        await runner.run(input_=_image_input(tiny_images))

    assert "image_estimate_unreliable" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_text_only_input_reports_zero_images(result):
    """純文字輸入的分項：image 為 0，text 為主，other 為 message overhead。"""
    events = []
    runner = _build_runner(events, result)

    await runner.run(input_=[{"role": "user", "content": "請用一句話介紹你自己"}])

    fields = _field(events, "estimate_ready")
    assert fields["image_tok"] == 0
    assert fields["image_count"] == 0
    assert fields["other_tok"] == PER_MESSAGE_OVERHEAD
    assert fields["text_tok"] > 0


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_wait_order(result):
    """等待順序：模型層限制 → 全域 RPM → 併發名額。

    Phase 3 依使用者指示，把原本只存在於已棄用 with_global_limits 裡的全域 RPM
    接進本等待鏈（先前環境變數 RPM 對實際請求毫無作用）。global_rpm 的位置
    比照原 with_global_limits：模型層限制之後、併發名額之前。
    """
    events = []
    runner = _build_runner(events, result)

    await runner.run(input_="hi")

    stages = [f.get("stage") for name, f in events if name == "acquired"]
    assert stages == [
        "build_system",
        "umbrella_tpm",
        f"model_tpm:{TEST_MODEL}",
        "model_rpm_chain",
        "global_rpm",
    ]
