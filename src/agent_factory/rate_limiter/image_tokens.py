"""影像 token 估算註冊表。

多模態輸入的 base64 影像若當成文字計數，單張相片即可估到百萬級 token。
本模組提供兩件事：

1. ``read_image_size``：只解碼 data URL 的前幾百 bytes，以純 Python 解析影像
   header 取得寬高（不引入 Pillow，也不完整解碼 base64）。
2. ``estimate_image_tokens``：依模型名查估算器，把寬高換算為該供應商的影像 token 數。

新增供應商支援時，只需在 ``IMAGE_TOKEN_ESTIMATORS`` 新增一個 entry，
不應修改本模組的走訪邏輯或 ``token_counter.py``（見 docs/architecture.md 原則 4）。

所有 fallback 一律取保守的高值（見 docs/architecture.md 原則 3）：高估只是請求稍慢，
低估會撞供應商 429，而本模組明列不做自動重試。
"""
from __future__ import annotations

import base64
import binascii
import logging
import math
import struct

from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger("rate.guard")

# --------------------------------------------------------------------------- #
# 影像 header 解析
# --------------------------------------------------------------------------- #

# 第一段解碼量。PNG（offset 16）、GIF（offset 6）、WebP（offset 26 附近）的尺寸欄位
# 都在固定且極前的位置，512 bytes 一律足夠。
HEADER_BYTES = 512

# JPEG 的 SOF marker 位置浮動，前面可能夾帶 APPn segment。相機直出相片的 EXIF
# 常內嵌縮圖，實測 SOF 落在 60–70 KB 處，512 bytes 讀不到。第一段解碼失敗時
# 才擴讀至此上限，仍遠小於整張影像（2.7 MB 相片約只解碼 5%）。
MAX_JPEG_HEADER_BYTES = 128 * 1024

# base64 每 4 個字元解出 3 bytes；長度必須為 4 的倍數才能解碼。
_BASE64_CHARS_PER_BLOCK = 4
_BYTES_PER_BASE64_BLOCK = 3

_DATA_URL_PREFIX = "data:"
_BASE64_MARKER = ";base64,"

# JPEG 的 Start-Of-Frame markers（0xFFC0–0xFFCF），但以下三個不是 SOF：
# 0xC4 = DHT（Huffman table）、0xC8 = JPG（保留）、0xCC = DAC（arithmetic coding）。
_JPEG_SOF_MARKERS = frozenset(
    set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
)


def _decode_data_url_header(image_url: str, max_bytes: int = HEADER_BYTES) -> Optional[bytes]:
    """自 data URL 取出開頭的位元組，只解碼 header 所需的長度。

    Args:
        image_url: 影像來源。僅處理 ``data:`` URL；遠端 http(s) URL 一律回傳 None。
        max_bytes: 最多解碼幾個位元組。

    Returns:
        影像檔開頭最多 max_bytes 個位元組；非 data URL 或無法解碼時回傳 None。
    """
    if not isinstance(image_url, str) or not image_url.startswith(_DATA_URL_PREFIX):
        # 遠端 URL 不在此處理：本模組不發任何網路請求。
        return None

    marker_index = image_url.find(_BASE64_MARKER)
    if marker_index == -1:
        return None

    payload = image_url[marker_index + len(_BASE64_MARKER):]

    blocks_needed = math.ceil(max_bytes / _BYTES_PER_BASE64_BLOCK)
    chars_needed = blocks_needed * _BASE64_CHARS_PER_BLOCK
    prefix = payload[:chars_needed]

    # 截斷至 4 的倍數，否則 b64decode 會因長度不合法而失敗。
    prefix = prefix[: len(prefix) - (len(prefix) % _BASE64_CHARS_PER_BLOCK)]
    if not prefix:
        return None

    try:
        return base64.b64decode(prefix)
    except (binascii.Error, ValueError):
        return None


def _parse_png_size(data: bytes) -> Optional[Tuple[int, int]]:
    """PNG：簽章後緊接 IHDR chunk，寬高為 offset 16／20 的 big-endian uint32。"""
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _parse_gif_size(data: bytes) -> Optional[Tuple[int, int]]:
    """GIF：邏輯螢幕描述子緊接 6 bytes 簽章，寬高為 little-endian uint16。"""
    if len(data) < 10:
        return None
    width, height = struct.unpack("<HH", data[6:10])
    return width, height


def _parse_jpeg_size(data: bytes) -> Optional[Tuple[int, int]]:
    """JPEG：自 SOI 後逐一走訪 segment，於 SOF marker 讀出高、寬（高在前）。"""
    offset = 2  # 跳過 SOI (0xFFD8)
    total = len(data)

    while offset + 1 < total:
        if data[offset] != 0xFF:
            # 不在 marker 邊界上，header 已損毀或被截斷。
            return None

        marker = data[offset + 1]
        offset += 2

        # 填充位元組：連續的 0xFF 需略過。
        if marker == 0xFF:
            offset -= 1
            continue
        # 無 payload 的 marker（RSTn、SOI、EOI、TEM）。
        if marker in (0x01, 0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue

        if offset + 2 > total:
            return None
        segment_length = struct.unpack(">H", data[offset:offset + 2])[0]

        if marker in _JPEG_SOF_MARKERS:
            # SOF payload：length(2) + precision(1) + height(2) + width(2)
            if offset + 7 > total:
                return None
            height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
            return width, height

        if segment_length < 2:
            return None
        offset += segment_length

    return None


def _parse_webp_size(data: bytes) -> Optional[Tuple[int, int]]:
    """WebP：RIFF 容器內依 chunk 型別分派（VP8 有損／VP8L 無損／VP8X 延伸）。"""
    if len(data) < 16 or data[8:12] != b"WEBP":
        return None

    chunk_type = data[12:16]

    if chunk_type == b"VP8 ":
        # keyframe：frame tag(3) + start code(3) + width(2) + height(2)，尺寸各取低 14 bits。
        if len(data) < 30 or data[23:26] != b"\x9d\x01\x2a":
            return None
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF

    if chunk_type == b"VP8L":
        # signature(1) + 14 bits width-1 + 14 bits height-1，皆為 little-endian bit stream。
        if len(data) < 25 or data[20] != 0x2F:
            return None
        bits = struct.unpack("<I", data[21:25])[0]
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height

    if chunk_type == b"VP8X":
        # flags(4) + canvas width-1(3, little-endian) + canvas height-1(3)
        if len(data) < 30:
            return None
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height

    return None


_SIZE_PARSERS = (
    (b"\x89PNG\r\n\x1a\n", _parse_png_size),
    (b"GIF8", _parse_gif_size),
    (b"\xff\xd8\xff", _parse_jpeg_size),
    (b"RIFF", _parse_webp_size),
)


def read_image_size(image_url: str) -> Optional[Tuple[int, int]]:
    """從 data URL 解析影像寬高，不解碼整張影像。

    先 base64 解碼前 HEADER_BYTES 個位元組，以純 Python 解析各格式的 header：
    JPEG（SOF marker）、PNG（IHDR）、WebP（VP8／VP8L／VP8X）、GIF（logical screen descriptor）。
    JPEG 的 SOF 位置浮動，第一段找不到時擴讀至 MAX_JPEG_HEADER_BYTES 再試一次。
    任何情況都不會完整解碼整張影像。

    Args:
        image_url: 影像來源，預期為 ``data:image/...;base64,...`` 形式。

    Returns:
        ``(width, height)``；遠端 http(s) URL、格式不支援或 header 截斷時回傳 None（不拋例外）。
    """
    data = _decode_data_url_header(image_url)
    if not data:
        return None

    for signature, parser in _SIZE_PARSERS:
        if not data.startswith(signature):
            continue

        size = _run_parser(parser, data)
        if size is None and parser is _parse_jpeg_size:
            extended = _decode_data_url_header(image_url, MAX_JPEG_HEADER_BYTES)
            if extended and len(extended) > len(data):
                size = _run_parser(parser, extended)
        return size

    return None


def _run_parser(parser, data: bytes) -> Optional[Tuple[int, int]]:
    """執行單一解析器，畸形輸入回傳 None 而非拋例外。"""
    try:
        size = parser(data)
    except (struct.error, IndexError):
        return None

    if size and size[0] > 0 and size[1] > 0:
        return size
    return None


# --------------------------------------------------------------------------- #
# OpenAI：tile-based（GPT-4o／GPT-4.1／GPT-4.5／o1／o3 家族）
# --------------------------------------------------------------------------- #

TILE_SIZE_PX = 512
TILE_TARGET_SHORT_SIDE_PX = 768
TILE_MAX_SQUARE_PX = 2048

# 失控保護：正常縮放路徑（長邊 2048、短邊 768）最多 4×2 = 8 塊，
# 極端長寬比會超過。設一個明顯高於實務值的上限，僅用於擋住畸形尺寸。
MAX_TILE_COUNT = 64

# 尺寸不可得時採用的保守塊數：文件描述的縮放上限 2048×768 → ceil(2048/512) × ceil(768/512)。
TILE_UNKNOWN_SIZE_COUNT = 8


def _tile_based_tokens(
    width: int,
    height: int,
    detail: str,
    *,
    base_tokens: int,
    tile_tokens: int,
) -> int:
    """OpenAI tile-based 影像 token 換算。

    文件描述的步驟：(1) 等比縮放至可容於 2048×2048 方框內；(2) 再等比縮放使短邊為 768px；
    (3) 計算涵蓋該影像所需的 512px 方塊數。``detail="low"`` 只計 base tokens，不計方塊。

    來源：https://developers.openai.com/api/docs/guides/images-vision
    查閱日期：2026-08-20
    """
    if detail == "low":
        return base_tokens

    scale = min(1.0, TILE_MAX_SQUARE_PX / max(width, height))
    scaled_width = width * scale
    scaled_height = height * scale

    short_side = min(scaled_width, scaled_height)
    if short_side > 0:
        short_side_scale = TILE_TARGET_SHORT_SIDE_PX / short_side
        scaled_width *= short_side_scale
        scaled_height *= short_side_scale

    tiles = math.ceil(scaled_width / TILE_SIZE_PX) * math.ceil(scaled_height / TILE_SIZE_PX)
    tiles = min(tiles, MAX_TILE_COUNT)

    return base_tokens + tiles * tile_tokens


# --------------------------------------------------------------------------- #
# OpenAI：patch-based（GPT-5 家族、gpt-4.1-mini／nano、o4-mini）
# --------------------------------------------------------------------------- #

PATCH_SIZE_PX = 32
PATCH_BUDGET = 1536


def _patch_based_tokens(
    width: int,
    height: int,
    detail: str,
    *,
    multiplier: float,
    patch_budget: int = PATCH_BUDGET,
) -> int:
    """OpenAI patch-based 影像 token 換算。

    文件描述的步驟：
    (A) ``original_patch_count = ceil(width/32) × ceil(height/32)``；
    (B) 若超出 patch budget，計算 shrink factor
        ``sqrt((32² × budget) / (width × height))``，再乘上寬高各自的
        「取整損失比」取小者，得 adjusted shrink factor；
    (C) 以縮放後尺寸重算 patch 數；
    (D) 乘上模型 multiplier。

    文件的兩組範例（budget 1536）可用於驗證本實作：
    1024×1024 → 1024 patches；1800×2400 → 縮至 1056×1408 → 1452 patches。

    來源：https://developers.openai.com/api/docs/guides/images-vision
    查閱日期：2026-08-20
    """
    patches = math.ceil(width / PATCH_SIZE_PX) * math.ceil(height / PATCH_SIZE_PX)

    if patches > patch_budget:
        shrink = math.sqrt((PATCH_SIZE_PX ** 2 * patch_budget) / (width * height))

        scaled_width_patches = width * shrink / PATCH_SIZE_PX
        scaled_height_patches = height * shrink / PATCH_SIZE_PX
        adjusted = shrink * min(
            math.floor(scaled_width_patches) / scaled_width_patches,
            math.floor(scaled_height_patches) / scaled_height_patches,
        )

        resized_width = width * adjusted
        resized_height = height * adjusted
        patches = math.ceil(resized_width / PATCH_SIZE_PX) * math.ceil(
            resized_height / PATCH_SIZE_PX
        )
        # 縮放後理論上不會超出 budget；仍夾一次以防浮點誤差。
        patches = min(patches, patch_budget)

    return math.ceil(patches * multiplier)


# --------------------------------------------------------------------------- #
# Google Gemini
# --------------------------------------------------------------------------- #

GEMINI_TOKENS_PER_TILE = 258
GEMINI_SMALL_IMAGE_MAX_PX = 384
GEMINI_CROP_DIVISOR = 1.5
MAX_GEMINI_TILE_COUNT = 64

# 尺寸不可得時採用的保守塊數（4×4 網格）。
GEMINI_UNKNOWN_SIZE_TILE_COUNT = 16


def _gemini_tokens(width: int, height: int, detail: str) -> int:
    """Gemini 影像 token 換算。

    文件描述：兩邊皆 ≤ 384px 的影像固定 258 tokens；較大的影像切成 tile，每塊 258 tokens。
    切塊單位約為 ``floor(min(width, height) / 1.5)``，寬高各自除以該單位後相乘得塊數。
    文件範例：960×540 → crop unit 360 → 3×2 = 6 塊 → 1548 tokens。

    來源：https://ai.google.dev/gemini-api/docs/image-understanding
    查閱日期：2026-08-20
    """
    if width <= GEMINI_SMALL_IMAGE_MAX_PX and height <= GEMINI_SMALL_IMAGE_MAX_PX:
        return GEMINI_TOKENS_PER_TILE

    crop_unit = math.floor(min(width, height) / GEMINI_CROP_DIVISOR)
    if crop_unit <= 0:
        return GEMINI_TOKENS_PER_TILE

    tiles = math.ceil(width / crop_unit) * math.ceil(height / crop_unit)
    tiles = min(tiles, MAX_GEMINI_TILE_COUNT)

    return tiles * GEMINI_TOKENS_PER_TILE


# --------------------------------------------------------------------------- #
# 註冊表
# --------------------------------------------------------------------------- #

def _tile_estimator(base_tokens: int, tile_tokens: int) -> Callable[[int, int, str], int]:
    def estimator(width: int, height: int, detail: str) -> int:
        return _tile_based_tokens(
            width, height, detail, base_tokens=base_tokens, tile_tokens=tile_tokens
        )

    return estimator


def _patch_estimator(multiplier: float) -> Callable[[int, int, str], int]:
    def estimator(width: int, height: int, detail: str) -> int:
        return _patch_based_tokens(width, height, detail, multiplier=multiplier)

    return estimator


# key 為模型名前綴，value 簽名為 (width, height, detail) -> tokens。
# 比對時取「最長符合的前綴」，因此 gpt-4.1-mini（patch-based）不會被 gpt-4.1（tile-based）攔截。
IMAGE_TOKEN_ESTIMATORS: Dict[str, Callable[[int, int, str], int]] = {
    # --- OpenAI tile-based ---
    "gpt-4o-mini": _tile_estimator(base_tokens=2833, tile_tokens=5667),
    "gpt-4o": _tile_estimator(base_tokens=85, tile_tokens=170),
    "gpt-4.1": _tile_estimator(base_tokens=85, tile_tokens=170),
    "gpt-4.5": _tile_estimator(base_tokens=85, tile_tokens=170),
    "o1": _tile_estimator(base_tokens=75, tile_tokens=150),
    "o3": _tile_estimator(base_tokens=75, tile_tokens=150),
    # --- OpenAI patch-based ---
    "gpt-4.1-mini": _patch_estimator(multiplier=1.62),
    "gpt-4.1-nano": _patch_estimator(multiplier=2.46),
    "gpt-5-mini": _patch_estimator(multiplier=1.62),
    "gpt-5-nano": _patch_estimator(multiplier=2.46),
    "gpt-5.4-mini": _patch_estimator(multiplier=1.62),
    "gpt-5.4-nano": _patch_estimator(multiplier=2.46),
    "o4-mini": _patch_estimator(multiplier=1.72),
    # --- Google Gemini ---
    "gemini": _gemini_tokens,
}

# 每個估算器在「尺寸不可得」時採用的保守 token 數（取 high-detail 上限）。
UNKNOWN_SIZE_TOKENS: Dict[str, int] = {
    "gpt-4o-mini": 2833 + TILE_UNKNOWN_SIZE_COUNT * 5667,
    "gpt-4o": 85 + TILE_UNKNOWN_SIZE_COUNT * 170,
    "gpt-4.1": 85 + TILE_UNKNOWN_SIZE_COUNT * 170,
    "gpt-4.5": 85 + TILE_UNKNOWN_SIZE_COUNT * 170,
    "o1": 75 + TILE_UNKNOWN_SIZE_COUNT * 150,
    "o3": 75 + TILE_UNKNOWN_SIZE_COUNT * 150,
    "gpt-4.1-mini": math.ceil(PATCH_BUDGET * 1.62),
    "gpt-4.1-nano": math.ceil(PATCH_BUDGET * 2.46),
    "gpt-5-mini": math.ceil(PATCH_BUDGET * 1.62),
    "gpt-5-nano": math.ceil(PATCH_BUDGET * 2.46),
    "gpt-5.4-mini": math.ceil(PATCH_BUDGET * 1.62),
    "gpt-5.4-nano": math.ceil(PATCH_BUDGET * 2.46),
    "o4-mini": math.ceil(PATCH_BUDGET * 1.72),
    "gemini": GEMINI_UNKNOWN_SIZE_TILE_COUNT * GEMINI_TOKENS_PER_TILE,
}

# 模型無對應估算器時的保守值。高於 tile-based 主流模型的 high-detail 上限（gpt-4o 為 1445），
# 同時遠低於把 base64 當文字計數的百萬級估值。
FALLBACK_IMAGE_TOKENS = 3000


def _match_prefix(model_name: str) -> Optional[str]:
    """取得模型名符合的最長估算器前綴。

    最長優先是必要的：``gpt-4.1-mini`` 走 patch-based，若被較短的 ``gpt-4.1``
    （tile-based）攔截就會用錯公式。
    """
    if not model_name:
        return None

    matches = [prefix for prefix in IMAGE_TOKEN_ESTIMATORS if model_name.startswith(prefix)]
    if not matches:
        return None

    return max(matches, key=len)


def estimate_image_tokens(image_url: str, model_name: str, detail: str = "auto") -> int:
    """估算單張影像的 token 成本。

    Args:
        image_url: 影像來源，預期為 base64 data URL。
        model_name: 模型名稱，以最長前綴比對估算器。
        detail: ``"auto"`` / ``"low"`` / ``"high"``。``"auto"`` 視同 ``"high"``（保守取高）。

    Returns:
        該影像的估算 token 數。尺寸不可得時取該模型家族的保守預設值；
        模型無對應估算器時取 FALLBACK_IMAGE_TOKENS。兩者皆會 log warning。
    """
    prefix = _match_prefix(model_name)

    if prefix is None:
        logger.warning(
            "no_image_estimator model=%s fallback_tokens=%d", model_name, FALLBACK_IMAGE_TOKENS
        )
        return FALLBACK_IMAGE_TOKENS

    size = read_image_size(image_url)
    if size is None:
        conservative = UNKNOWN_SIZE_TOKENS[prefix]
        logger.warning(
            "image_size_unavailable model=%s estimator=%s conservative_tokens=%d",
            model_name,
            prefix,
            conservative,
        )
        return conservative

    width, height = size
    return IMAGE_TOKEN_ESTIMATORS[prefix](width, height, detail)
