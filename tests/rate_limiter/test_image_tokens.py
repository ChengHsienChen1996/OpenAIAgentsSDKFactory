"""image_tokens 的單元測試。

期望值的來源：

- patch-based：OpenAI 官方文件列出的兩組已算好的範例（1024×1024 → 1024 patches；
  1800×2400 → 縮至 1056×1408 → 1452 patches）。
  https://developers.openai.com/api/docs/guides/images-vision（查閱 2026-08-20）
- Gemini：官方文件的 960×540 → 6 塊 → 1548 tokens 範例。
  https://ai.google.dev/gemini-api/docs/image-understanding（查閱 2026-08-20）
- tile-based：官方文件未給已算好的範例，期望值依文件描述的三步驟逐步推導，
  各測試的註解寫明推導過程，以便人工複查公式是否抄錯。
"""
import base64
import socket
import struct

import pytest

from agent_factory.rate_limiter.image_tokens import (
    FALLBACK_IMAGE_TOKENS,
    GEMINI_TOKENS_PER_TILE,
    IMAGE_TOKEN_ESTIMATORS,
    MAX_GEMINI_TILE_COUNT,
    MAX_TILE_COUNT,
    PATCH_BUDGET,
    UNKNOWN_SIZE_TOKENS,
    _match_prefix,
    estimate_image_tokens,
    read_image_size,
)

# tiny_images fixture 的尺寸（見 tests/conftest.py）
FIXTURE_WIDTH = 2
FIXTURE_HEIGHT = 3


def data_url(payload: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64," + base64.b64encode(payload).decode("utf-8")


# --------------------------------------------------------------------------- #
# 尺寸解析
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("fmt", ["jpeg", "png", "webp", "gif"])
def test_read_image_size_parses_all_supported_formats(tiny_images, fmt):
    """四種格式的最小合法 header 都能解析出正確寬高。"""
    size = read_image_size(data_url(tiny_images[fmt]))

    assert size == (FIXTURE_WIDTH, FIXTURE_HEIGHT)


def _webp_lossless_bytes(width: int, height: int) -> bytes:
    """VP8L（無損）：signature 0x2F 後為 14 bits width-1 + 14 bits height-1。"""
    bits = (width - 1) | ((height - 1) << 14)
    payload = bytes([0x2F]) + struct.pack("<I", bits)
    chunk = b"VP8L" + struct.pack("<I", len(payload)) + payload
    body = b"WEBP" + chunk
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _webp_extended_bytes(width: int, height: int) -> bytes:
    """VP8X（延伸）：flags(4) + canvas width-1(3, LE) + canvas height-1(3, LE)。"""
    payload = bytes(4) + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    chunk = b"VP8X" + struct.pack("<I", len(payload)) + payload
    body = b"WEBP" + chunk
    return b"RIFF" + struct.pack("<I", len(body)) + body


@pytest.mark.parametrize("builder", [_webp_lossless_bytes, _webp_extended_bytes])
def test_read_image_size_parses_webp_variants(builder):
    """VP8 之外的 WebP 變體（VP8L 無損、VP8X 延伸）同樣要能解析。"""
    size = read_image_size(data_url(builder(640, 480), "image/webp"))

    assert size == (640, 480)


def test_read_image_size_finds_sof_beyond_first_chunk(tiny_images):
    """JPEG 的 SOF 落在第一段解碼範圍外時，應擴讀後解析成功。

    相機直出相片的 EXIF 常內嵌縮圖，實測 SOF 位於 60–70 KB 處。
    單一 segment 的長度欄位是 uint16（上限 65535），真實檔案是以多個 segment 累積，
    此處比照以兩個 30 KB 的 APP1 segment 模擬。
    """
    segment_body = 30 * 1024
    app1 = (b"\xff\xe1" + struct.pack(">H", segment_body + 2) + bytes(segment_body)) * 2
    sof = (
        b"\xff\xc0"
        + struct.pack(">H", 11)
        + bytes([8])
        + struct.pack(">HH", 300, 400)
        + bytes([1, 1, 0x11, 0])
    )
    payload = b"\xff\xd8" + app1 + sof + b"\xff\xd9"

    assert len(payload) > 60 * 1024
    assert read_image_size(data_url(payload)) == (400, 300)


def test_read_image_size_returns_none_for_remote_url_without_network():
    """遠端 URL 一律回傳 None，且不得發出任何網路連線。"""
    original_socket = socket.socket

    def _fail(*args, **kwargs):
        raise AssertionError("read_image_size 不應建立網路連線")

    socket.socket = _fail
    try:
        assert read_image_size("https://example.com/photo.jpg") is None
        assert read_image_size("http://example.com/photo.png") is None
    finally:
        socket.socket = original_socket


@pytest.mark.parametrize(
    "bad_input",
    [
        "",
        "not-a-url",
        "data:image/png,notbase64",                      # 缺 ;base64, 標記
        "data:image/png;base64,####",                    # 非法 base64
        "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n").decode(),  # header 截斷
        "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0\x00").decode(),  # SOF 缺失
        "data:application/pdf;base64," + base64.b64encode(b"%PDF-1.4").decode(),     # 不支援的格式
    ],
)
def test_read_image_size_returns_none_on_malformed_input(bad_input):
    """畸形、截斷或不支援的輸入回傳 None，不拋例外。"""
    assert read_image_size(bad_input) is None


def test_read_image_size_rejects_zero_dimensions():
    """尺寸為 0 的 header 視為無效。"""
    zero_gif = b"GIF89a" + struct.pack("<HH", 0, 0) + bytes(3)

    assert read_image_size(data_url(zero_gif, "image/gif")) is None


# --------------------------------------------------------------------------- #
# 換算公式：對照官方文件的已知範例
# --------------------------------------------------------------------------- #

def _patch_count(width: int, height: int) -> int:
    """以 multiplier=1 反推 patch 數，供對照官方文件的 patch 範例。"""
    return IMAGE_TOKEN_ESTIMATORS["gpt-5-mini"](width, height, "high") / 1.62


@pytest.mark.parametrize(
    "width,height,expected_patches",
    [
        # 官方文件範例：未超出 1536 budget，不縮放 → ceil(1024/32)² = 32×32
        (1024, 1024, 1024),
        # 官方文件範例：超出 budget → 縮至 1056×1408 → 33×44
        (1800, 2400, 1452),
    ],
)
def test_patch_based_matches_documented_examples(width, height, expected_patches):
    """patch 數必須與 OpenAI 文件列出的範例逐一相符。

    這是驗證 shrink factor 公式是否抄對的唯一客觀依據 —— 期望值直接取自官方文件，
    非由本實作推導。
    """
    from agent_factory.rate_limiter.image_tokens import _patch_based_tokens

    actual = _patch_based_tokens(width, height, "high", multiplier=1.0)

    assert actual == expected_patches


def test_patch_based_never_exceeds_budget():
    """超大影像的 patch 數不得超過 budget。"""
    from agent_factory.rate_limiter.image_tokens import _patch_based_tokens

    actual = _patch_based_tokens(20000, 20000, "high", multiplier=1.0)

    assert actual <= PATCH_BUDGET


def test_gemini_matches_documented_example():
    """Gemini 960×540 → crop unit 360 → 3×2 = 6 塊 → 1548 tokens（官方文件範例）。"""
    tokens = IMAGE_TOKEN_ESTIMATORS["gemini"](960, 540, "high")

    assert tokens == 6 * GEMINI_TOKENS_PER_TILE == 1548


def test_gemini_small_image_is_single_tile():
    """兩邊皆 ≤ 384px 的影像固定 258 tokens（官方文件）。"""
    assert IMAGE_TOKEN_ESTIMATORS["gemini"](384, 384, "high") == GEMINI_TOKENS_PER_TILE


def test_gemini_clamps_tile_count():
    """超大影像的塊數受 MAX_GEMINI_TILE_COUNT 夾住。"""
    tokens = IMAGE_TOKEN_ESTIMATORS["gemini"](100000, 100, "high")

    assert tokens <= MAX_GEMINI_TILE_COUNT * GEMINI_TOKENS_PER_TILE


@pytest.mark.parametrize(
    "width,height,expected",
    [
        # 1024×1024：已在 2048 方框內 → 短邊 1024 縮至 768 → 768×768
        # → ceil(768/512)=2，2×2=4 塊 → 85 + 4×170 = 765
        (1024, 1024, 765),
        # 2048×4096：縮至 1024×2048 → 短邊 1024 縮至 768 → 768×1536
        # → 2×3=6 塊 → 85 + 6×170 = 1105
        (2048, 4096, 1105),
        # 4000×3000（實測相片尺寸）：縮至 2048×1536 → 短邊 1536 縮至 768 → 1024×768
        # → 2×2=4 塊 → 85 + 4×170 = 765
        (4000, 3000, 765),
    ],
)
def test_tile_based_follows_documented_steps(width, height, expected):
    """tile-based 依文件三步驟推導的期望值（推導過程見各參數註解）。"""
    assert IMAGE_TOKEN_ESTIMATORS["gpt-4o"](width, height, "high") == expected


@pytest.mark.parametrize(
    "width,height,expected",
    [
        # 短邊已小於 768，不放大 → 1 塊 → 2833 + 1×5667
        (256, 256, 8500),
        (512, 512, 8500),
        # 短邊 1024 > 768，縮小至 768×768 → 4 塊 → 2833 + 4×5667
        (1024, 1024, 25501),
        # 短邊 1024 → 768，得 768×1152 → 2×3 = 6 塊 → 2833 + 6×5667
        (1024, 1536, 36835),
    ],
)
def test_tile_based_matches_measured_billing(width, height, expected):
    """期望值取自 gpt-4o-mini 的實際計費，非由文件推導。

    來源：logs/2026-08-20_test_multimodal-estimation-validation.md
    這組數字是唯一能抓出「短邊縮放方向」讀法錯誤的依據 —— 對照官方文件無法發現，
    因為文件的措辭對放大與否是有歧義的。
    """
    assert IMAGE_TOKEN_ESTIMATORS["gpt-4o-mini"](width, height, "high") == expected


def test_tile_based_does_not_upscale_small_images():
    """小圖不得被放大 —— 放大會讓估值變成實際計費的 3 倍。"""
    small = IMAGE_TOKEN_ESTIMATORS["gpt-4o"](256, 256, "high")
    one_tile = 85 + 170

    assert small == one_tile


def test_tile_based_low_detail_costs_base_only():
    """detail="low" 只計 base tokens，不論影像多大。"""
    assert IMAGE_TOKEN_ESTIMATORS["gpt-4o"](8000, 8000, "low") == 85


def test_tile_based_clamps_tile_count():
    """極端長寬比的塊數受 MAX_TILE_COUNT 夾住。"""
    tokens = IMAGE_TOKEN_ESTIMATORS["gpt-4o"](200000, 768, "high")

    assert tokens <= 85 + MAX_TILE_COUNT * 170


# --------------------------------------------------------------------------- #
# 註冊表比對
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "model_name,expected_prefix",
    [
        ("gpt-4o", "gpt-4o"),
        ("gpt-4o-2024-11-20", "gpt-4o"),
        ("gpt-4o-mini", "gpt-4o-mini"),
        ("gpt-4.1", "gpt-4.1"),
        ("gpt-4.1-2025-04-14", "gpt-4.1"),
        ("gpt-4.1-mini", "gpt-4.1-mini"),
        ("gpt-4.1-nano", "gpt-4.1-nano"),
        ("gemini-2.5-flash", "gemini"),
        ("o4-mini", "o4-mini"),
    ],
)
def test_match_prefix_prefers_longest(model_name, expected_prefix):
    """前綴比對取最長符合者。

    這是本模組最容易出錯的地方：gpt-4.1-mini 走 patch-based，
    若被較短的 gpt-4.1（tile-based）攔截就會套錯公式。
    """
    assert _match_prefix(model_name) == expected_prefix


def test_gpt_41_and_gpt_41_mini_use_different_formulas():
    """同尺寸下 gpt-4.1（tile）與 gpt-4.1-mini（patch）的結果必須不同。

    若兩者相同，代表前綴比對把 mini 錯配到 tile-based 估算器。
    """
    tile = estimate_image_tokens(_gpt_url(), "gpt-4.1", "high")
    patch = estimate_image_tokens(_gpt_url(), "gpt-4.1-mini", "high")

    assert tile != patch


def _gpt_url() -> str:
    """640×480 的 VP8L WebP，供比對兩種公式使用。"""
    return data_url(_webp_lossless_bytes(640, 480), "image/webp")


# --------------------------------------------------------------------------- #
# fallback 行為
# --------------------------------------------------------------------------- #

def test_unknown_model_uses_fallback(caplog):
    """模型無對應估算器時取 FALLBACK_IMAGE_TOKENS 並 log warning。"""
    with caplog.at_level("WARNING", logger="rate.guard"):
        tokens = estimate_image_tokens(_gpt_url(), "glm-ocr-optimized:latest", "high")

    assert tokens == FALLBACK_IMAGE_TOKENS
    assert "no_image_estimator" in caplog.text


def test_unparseable_size_uses_conservative_default(caplog):
    """尺寸不可得時取該家族的保守預設值並 log warning。"""
    with caplog.at_level("WARNING", logger="rate.guard"):
        tokens = estimate_image_tokens("https://example.com/photo.jpg", "gpt-4o", "high")

    assert tokens == UNKNOWN_SIZE_TOKENS["gpt-4o"]
    assert "image_size_unavailable" in caplog.text


@pytest.mark.parametrize("prefix", sorted(IMAGE_TOKEN_ESTIMATORS))
def test_unknown_size_default_is_conservative(prefix):
    """每個家族的「尺寸不可得」預設值都不得低於一張常見尺寸影像的實際估值。

    對應 architecture.md 原則 3：fallback 一律取保守高值，低估會撞供應商 429。
    """
    typical = IMAGE_TOKEN_ESTIMATORS[prefix](1024, 1024, "high")

    assert UNKNOWN_SIZE_TOKENS[prefix] >= typical


def test_every_estimator_has_unknown_size_default():
    """註冊表與保守預設值表必須一一對應，新增估算器時不得漏填。"""
    assert set(IMAGE_TOKEN_ESTIMATORS) == set(UNKNOWN_SIZE_TOKENS)


def test_real_photo_estimate_is_orders_of_magnitude_below_text_counting(tiny_images):
    """本 phase 的核心目標：影像估值應為數百至數千，而非百萬級。"""
    tokens = estimate_image_tokens(data_url(tiny_images["png"], "image/png"), "gpt-4o", "auto")

    assert 0 < tokens < 10_000
