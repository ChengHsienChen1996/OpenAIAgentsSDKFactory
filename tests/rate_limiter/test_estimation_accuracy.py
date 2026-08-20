"""估算分項歸屬與加總的單元測試（不需外部服務）。

與 test_token_counter.py 的分工：那邊驗證「走訪規則是否正確」，
這邊驗證「分項數字是否可信」—— 分項會進 trace，是誤差量測的唯一資料來源，
歸屬錯誤會讓實測統計得出錯誤結論。
"""
import base64
import struct
import zlib

import pytest

from agent_factory.rate_limiter.image_tokens import (
    IMAGE_TOKEN_ESTIMATORS,
    estimate_image_tokens,
)
from agent_factory.rate_limiter.token_counter import (
    PER_MESSAGE_OVERHEAD,
    UNKNOWN_ITEM_TOKENS,
    count_tokens,
    estimate_input_tokens,
)

TILE_MODEL = "gpt-4o"          # tile-based
PATCH_MODEL = "gpt-4.1-mini"   # patch-based
GEMINI_MODEL = "gemini-2.5-flash"


def png_of_size(width: int, height: int) -> bytes:
    """產生指定尺寸、完整可解碼的 PNG（IHDR + IDAT + IEND）。

    僅有 header 的檔案雖足以測試尺寸解析，但無法送進真實供應商；
    此處產生完整檔案，讓同一組素材也能用於實測。
    """
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    raw = b"".join(
        bytes([0]) + b"".join(bytes([(x * 7) % 256, (y * 5) % 256, 128]) for x in range(width))
        for y in range(height)
    )
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def png_data_url(width: int, height: int) -> str:
    return "data:image/png;base64," + base64.b64encode(png_of_size(width, height)).decode("utf-8")


def image_message(url: str, detail: str = "auto") -> dict:
    return {"role": "user", "content": [{"type": "input_image", "detail": detail, "image_url": url}]}


# --------------------------------------------------------------------------- #
# 分項加總
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("model", [TILE_MODEL, PATCH_MODEL, GEMINI_MODEL])
def test_parts_sum_to_total_for_every_estimator_family(model):
    """三種估算器家族下，分項加總都必須等於 total。"""
    url = png_data_url(64, 64)
    messages = [
        image_message(url),
        {"role": "user", "content": "描述這張圖"},
        {"type": "function_call_output", "call_id": "1", "output": "ok"},
        {"type": "item_reference", "id": "x"},
    ]

    estimate = estimate_input_tokens(messages, model)

    assert estimate.total == estimate.text_tokens + estimate.image_tokens + estimate.other_tokens


def test_empty_list_produces_zero_estimate():
    estimate = estimate_input_tokens([], TILE_MODEL)

    assert estimate.total == 0
    assert estimate.image_count == 0
    assert estimate.has_unknown_items is False


# --------------------------------------------------------------------------- #
# 分項歸屬
# --------------------------------------------------------------------------- #

def test_text_goes_only_to_text_tokens():
    """純文字輸入不得產生 image_tokens。"""
    estimate = estimate_input_tokens([{"role": "user", "content": "hello world"}], TILE_MODEL)

    assert estimate.image_tokens == 0
    assert estimate.text_tokens == count_tokens("hello world", TILE_MODEL)
    assert estimate.other_tokens == PER_MESSAGE_OVERHEAD


def test_image_goes_only_to_image_tokens():
    """純影像輸入不得產生 text_tokens。"""
    url = png_data_url(64, 64)

    estimate = estimate_input_tokens([image_message(url)], TILE_MODEL)

    assert estimate.text_tokens == 0
    assert estimate.image_tokens == estimate_image_tokens(url, TILE_MODEL, "auto")


def test_unknown_item_goes_only_to_other_tokens():
    """未知 item 只計入 other，且標記 has_unknown_items。"""
    estimate = estimate_input_tokens([{"type": "image_generation_call", "id": "1"}], TILE_MODEL)

    assert estimate.text_tokens == 0
    assert estimate.image_tokens == 0
    assert estimate.other_tokens == UNKNOWN_ITEM_TOKENS
    assert estimate.has_unknown_items is True


def test_mixed_content_attributes_each_part_independently():
    """混合輸入時，三個分項各自獨立且可個別驗算。"""
    url = png_data_url(64, 64)
    text = "描述這張圖"
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "input_image", "detail": "auto", "image_url": url},
                {"type": "input_text", "text": text},
            ],
        }
    ]

    estimate = estimate_input_tokens(messages, TILE_MODEL)

    assert estimate.text_tokens == count_tokens(text, TILE_MODEL)
    assert estimate.image_tokens == estimate_image_tokens(url, TILE_MODEL, "auto")
    assert estimate.other_tokens == PER_MESSAGE_OVERHEAD
    assert estimate.image_count == 1


def test_image_tokens_scale_with_dimensions():
    """較大的影像應產生較高的估值 —— 否則尺寸解析形同虛設。"""
    small = estimate_input_tokens([image_message(png_data_url(64, 64))], PATCH_MODEL)
    large = estimate_input_tokens([image_message(png_data_url(512, 512))], PATCH_MODEL)

    assert large.image_tokens > small.image_tokens


def test_multiple_images_accumulate():
    """多張影像的 image_tokens 應為各張之和。"""
    url = png_data_url(64, 64)
    single = estimate_input_tokens([image_message(url)], TILE_MODEL)
    triple = estimate_input_tokens([image_message(url), image_message(url), image_message(url)], TILE_MODEL)

    assert triple.image_count == 3
    assert triple.image_tokens == 3 * single.image_tokens


def test_low_detail_reduces_tile_based_estimate():
    """tile-based 模型的 detail=low 應明顯降低估值。"""
    url = png_data_url(512, 512)
    high = estimate_input_tokens([image_message(url, "high")], TILE_MODEL)
    low = estimate_input_tokens([image_message(url, "low")], TILE_MODEL)

    assert low.image_tokens < high.image_tokens


# --------------------------------------------------------------------------- #
# 估算值的量級：本輪優化的核心目標
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("model", sorted(IMAGE_TOKEN_ESTIMATORS))
def test_estimate_stays_in_reasonable_magnitude(model):
    """所有估算器對 1024×1024 影像的估值都應落在數百至數萬之間。

    低於數百代表公式可能漏算（低估會撞 429）；高於數萬代表可能又退回了文字計數。
    """
    tokens = IMAGE_TOKEN_ESTIMATORS[model](1024, 1024, "high")

    assert 100 < tokens < 100_000, f"{model} 估值異常：{tokens}"


def test_typical_multimodal_request_is_dominated_by_image():
    """典型多模態請求：影像佔絕大多數，文字只有個位數到數十。"""
    messages = [
        image_message(png_data_url(1024, 1024)),
        {"role": "user", "content": "Table Recognition:"},
    ]

    estimate = estimate_input_tokens(messages, TILE_MODEL)

    assert estimate.image_tokens > 10 * estimate.text_tokens
    assert estimate.text_tokens < 50
