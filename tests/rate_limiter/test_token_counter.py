"""token_counter 的單元測試。

重點在兩件事：
1. base64 影像絕不進文字計數（本 phase 的核心目標）。
2. 未知／畸形結構一律保守高估並標記，不得靜默略過 —— 略過等同低估。
"""
import base64

import pytest

from agent_factory.rate_limiter.image_tokens import FALLBACK_IMAGE_TOKENS
from agent_factory.rate_limiter.token_counter import (
    PER_MESSAGE_OVERHEAD,
    UNKNOWN_ITEM_TOKENS,
    InputTokenEstimate,
    count_tokens,
    estimate_input_tokens,
)

MODEL = "gpt-4o"


def image_data_url(payload: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(payload).decode("utf-8")


def image_message(data_url: str, detail: str = "auto") -> dict:
    return {
        "role": "user",
        "content": [{"type": "input_image", "detail": detail, "image_url": data_url}],
    }


# --------------------------------------------------------------------------- #
# count_tokens 簽名維持不變
# --------------------------------------------------------------------------- #

def test_count_tokens_signature_unchanged():
    """count_tokens 仍接受 (text, model) 並回傳 int，外部使用不受影響。"""
    assert count_tokens("hello world", MODEL) > 0
    assert count_tokens("hello world") > 0
    assert count_tokens("") == 0
    assert count_tokens(None) == 0


# --------------------------------------------------------------------------- #
# 基本走訪
# --------------------------------------------------------------------------- #

def test_plain_string_counts_as_text():
    """字串輸入直接做文字計數，不加 message overhead。"""
    estimate = estimate_input_tokens("請用一句話介紹你自己", MODEL)

    assert estimate.text_tokens > 0
    assert estimate.image_tokens == 0
    assert estimate.other_tokens == 0
    assert estimate.image_count == 0
    assert estimate.has_unknown_items is False
    assert estimate.total == estimate.text_tokens


def test_none_input_returns_empty_estimate():
    """None 輸入回傳全零估算，不拋例外。"""
    estimate = estimate_input_tokens(None, MODEL)

    assert estimate == InputTokenEstimate()


def test_string_content_message_adds_overhead():
    """content 為字串的 message：文字計數 + 每則 message 的固定成本。"""
    text = "hello"
    estimate = estimate_input_tokens([{"role": "user", "content": text}], MODEL)

    assert estimate.text_tokens == count_tokens(text, MODEL)
    assert estimate.other_tokens == PER_MESSAGE_OVERHEAD
    assert estimate.total == estimate.text_tokens + PER_MESSAGE_OVERHEAD


def test_input_text_content_item_counted_as_text():
    """content 陣列中的 input_text 取 text 欄位計數。"""
    estimate = estimate_input_tokens(
        [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}], MODEL
    )

    assert estimate.text_tokens == count_tokens("hello", MODEL)
    assert estimate.image_count == 0


def test_multiple_messages_accumulate_overhead():
    """每則 message 各計一次 overhead。"""
    messages = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
    ]
    estimate = estimate_input_tokens(messages, MODEL)

    assert estimate.other_tokens == 3 * PER_MESSAGE_OVERHEAD


def test_total_always_equals_sum_of_parts():
    """分項加總必須等於 total，否則 trace 的分項數字無法採信。"""
    estimate = estimate_input_tokens(
        [
            {"role": "user", "content": "文字"},
            image_message(image_data_url(b"\x89PNG\r\n\x1a\n")),
            {"type": "function_call", "name": "f", "arguments": "{}", "call_id": "1"},
            {"type": "image_generation_call", "id": "x", "result": "AAAA", "status": "completed"},
        ],
        MODEL,
    )

    assert estimate.total == estimate.text_tokens + estimate.image_tokens + estimate.other_tokens


# --------------------------------------------------------------------------- #
# 影像：本 phase 的核心
# --------------------------------------------------------------------------- #

def test_image_never_counted_as_text(tiny_images):
    """input_image 完全不參與文字計數，image_url 的長度不影響 text_tokens。"""
    small = estimate_input_tokens([image_message(image_data_url(tiny_images["png"]))], MODEL)

    # 同一張影像，但 data URL 後面塞入大量填充字元
    padded_url = image_data_url(tiny_images["png"]) + "A" * 500_000
    padded = estimate_input_tokens([image_message(padded_url)], MODEL)

    assert small.text_tokens == 0
    assert padded.text_tokens == 0
    assert small.image_count == padded.image_count == 1


def test_large_image_estimate_stays_small(tiny_images):
    """大體積影像的估算值仍為數百至數千級，而非百萬級。"""
    # 以 1 MB 的填充模擬大檔（尺寸來自 header，與檔案大小無關）
    payload = tiny_images["png"] + b"\x00" * (1024 * 1024)
    estimate = estimate_input_tokens([image_message(image_data_url(payload))], MODEL)

    assert 0 < estimate.total < 10_000


def test_image_count_tracks_number_of_images(tiny_images):
    """image_count 應等於實際的影像 item 數。"""
    url = image_data_url(tiny_images["png"])
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "input_image", "detail": "auto", "image_url": url},
                {"type": "input_text", "text": "描述這些圖"},
                {"type": "input_image", "detail": "auto", "image_url": url},
            ],
        }
    ]
    estimate = estimate_input_tokens(messages, MODEL)

    assert estimate.image_count == 2
    assert estimate.text_tokens == count_tokens("描述這些圖", MODEL)


def test_unknown_model_image_uses_fallback(tiny_images):
    """模型無對應估算器時，影像走 FALLBACK_IMAGE_TOKENS。"""
    estimate = estimate_input_tokens(
        [image_message(image_data_url(tiny_images["png"]))], "glm-ocr-optimized:latest"
    )

    assert estimate.image_tokens == FALLBACK_IMAGE_TOKENS


# --------------------------------------------------------------------------- #
# 未知與畸形結構：必須保守高估，不得靜默略過
# --------------------------------------------------------------------------- #

def test_image_url_with_missing_type_is_unknown_not_text(tiny_images):
    """content item 帶 image_url 但 type 缺失 → 歸未知 item，絕不退回文字計數。

    退回文字計數就是本 phase 要修掉的原始 bug。
    """
    url = image_data_url(tiny_images["png"])
    messages = [{"role": "user", "content": [{"image_url": url}]}]

    estimate = estimate_input_tokens(messages, MODEL)

    assert estimate.text_tokens == 0
    assert estimate.other_tokens == PER_MESSAGE_OVERHEAD + UNKNOWN_ITEM_TOKENS
    assert estimate.has_unknown_items is True


def test_image_url_with_wrong_type_is_unknown_not_text(tiny_images):
    """type 為非預期值時同樣歸未知，不做文字計數。"""
    url = image_data_url(tiny_images["png"])
    messages = [{"role": "user", "content": [{"type": "image", "image_url": url}]}]

    estimate = estimate_input_tokens(messages, MODEL)

    assert estimate.text_tokens == 0
    assert estimate.has_unknown_items is True


@pytest.mark.parametrize(
    "malformed",
    [
        [{"role": "user"}],                          # message 缺 content
        [{"role": "user", "content": 12345}],        # content 型別不預期
        [{"role": "user", "content": [None]}],       # content item 非 dict
        ["not-a-dict"],                              # item 非 dict
        [42],                                        # item 非 dict
    ],
)
def test_malformed_structures_are_charged_not_skipped(malformed):
    """畸形結構不拋例外，但必須計入保守常數並標記 —— 靜默略過等同低估。"""
    estimate = estimate_input_tokens(malformed, MODEL)

    assert estimate.has_unknown_items is True
    assert estimate.total >= UNKNOWN_ITEM_TOKENS


def test_unsupported_input_type_is_charged():
    """input_ 既非字串也非序列時計入保守常數。"""
    estimate = estimate_input_tokens({"role": "user"}, MODEL)

    assert estimate.has_unknown_items is True
    assert estimate.total >= UNKNOWN_ITEM_TOKENS


# --------------------------------------------------------------------------- #
# 非 message item（tool call 等）
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "item,expected_text",
    [
        ({"type": "function_call", "call_id": "1", "name": "get_weather", "arguments": '{"city":"TPE"}'},
         "get_weather" + '{"city":"TPE"}'),
        ({"type": "function_call_output", "call_id": "1", "output": "sunny"}, "sunny"),
        ({"type": "local_shell_call_output", "id": "1", "output": "done"}, "done"),
        ({"type": "custom_tool_call_output", "call_id": "1", "output": "result"}, "result"),
        ({"type": "code_interpreter_call", "id": "1", "code": "print(1)", "container_id": "c",
          "outputs": None, "status": "completed"}, "print(1)"),
    ],
)
def test_tool_items_count_their_text_fields(item, expected_text):
    """tool call 與其結果的純字串欄位應做文字計數，且不計 message overhead。"""
    estimate = estimate_input_tokens([item], MODEL)

    assert estimate.has_unknown_items is False
    assert estimate.other_tokens == 0
    assert estimate.text_tokens > 0


def test_reasoning_item_counts_summary_text():
    """reasoning item 的 summary 為 [{"text": ...}] 形式，應取出計數。"""
    item = {
        "type": "reasoning",
        "id": "r1",
        "summary": [{"type": "summary_text", "text": "先確認輸入"}],
    }
    estimate = estimate_input_tokens([item], MODEL)

    assert estimate.text_tokens == count_tokens("先確認輸入", MODEL)
    assert estimate.has_unknown_items is False


def test_computer_call_output_is_not_text_counted():
    """computer_call_output 的 output 是截圖 dict（內含 image_url），不得做文字計數。

    若被當成字串掃描，base64 截圖會再次造成百萬級估值。
    """
    item = {
        "type": "computer_call_output",
        "call_id": "1",
        "output": {"type": "computer_screenshot", "image_url": "data:image/png;base64," + "A" * 100_000},
    }
    estimate = estimate_input_tokens([item], MODEL)

    assert estimate.text_tokens == 0
    assert estimate.other_tokens == UNKNOWN_ITEM_TOKENS
    assert estimate.has_unknown_items is True


def test_image_generation_call_is_not_text_counted():
    """image_generation_call 的 result 是 base64 影像，不得做文字計數。"""
    item = {
        "type": "image_generation_call",
        "id": "1",
        "result": "A" * 100_000,
        "status": "completed",
    }
    estimate = estimate_input_tokens([item], MODEL)

    assert estimate.text_tokens == 0
    assert estimate.has_unknown_items is True


def test_item_reference_is_conservative():
    """item_reference 指向看不到的內容，必須保守計入而非視為零成本。"""
    estimate = estimate_input_tokens([{"type": "item_reference", "id": "msg_123"}], MODEL)

    assert estimate.other_tokens == UNKNOWN_ITEM_TOKENS
    assert estimate.has_unknown_items is True


def test_realistic_multimodal_input_is_dominated_by_image(tiny_images):
    """實際的多模態輸入形態：影像 message + 文字 message。"""
    url = image_data_url(tiny_images["jpeg"])
    messages = [
        {"role": "user", "content": [{"type": "input_image", "detail": "auto", "image_url": url}]},
        {"role": "user", "content": "Table Recognition:"},
    ]
    estimate = estimate_input_tokens(messages, MODEL)

    assert estimate.image_count == 1
    assert estimate.image_tokens > estimate.text_tokens
    assert estimate.other_tokens == 2 * PER_MESSAGE_OVERHEAD
    assert estimate.has_unknown_items is False
