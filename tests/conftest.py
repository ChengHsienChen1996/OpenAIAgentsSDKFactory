"""共用 fixtures。

`mock_run_result` 與 `tiny_images` 在 Phase 1 尚未被使用，是 Phase 2 的
Task 2.2（影像尺寸解析）與 Task 2.4（預扣／退款校正）預留的測試基礎。
"""
import struct
import zlib

from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Dict, Iterable, Tuple

import pytest

from agent_factory.core import AgentFactory

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# tiny_images 的尺寸。刻意讓寬高不相等，寬高順序寫反的解析錯誤才會被測出來。
TINY_IMAGE_WIDTH = 2
TINY_IMAGE_HEIGHT = 3


@pytest.fixture
def sample_yaml_path() -> Path:
    """單元測試用最小 agent 設定的路徑（不需環境變數與網路）。"""
    return FIXTURES_DIR / "sample_agents.yaml"


@pytest.fixture
def sample_factory(sample_yaml_path: Path) -> AgentFactory:
    """自最小設定建立的 AgentFactory，供多個測試共用。"""
    return AgentFactory.create_factory_from_yaml(sample_yaml_path)


@pytest.fixture
def mock_run_result() -> Callable[[Iterable[Tuple[str, int]]], SimpleNamespace]:
    """回傳一個工廠函式，用來建立模擬的 ``RunResult``。

    參數為 ``(model_name, total_tokens)`` 序列，每組產生一筆 raw_response。
    `limits_guard_multi` 的校正邏輯只讀 ``raw_responses[*].usage.total_tokens``
    與 ``raw_responses[*].model``，故此處只模擬這兩個屬性。
    """

    def _make(usages: Iterable[Tuple[str, int]]) -> SimpleNamespace:
        raw_responses = [
            SimpleNamespace(model=model, usage=SimpleNamespace(total_tokens=total))
            for model, total in usages
        ]
        return SimpleNamespace(raw_responses=raw_responses, new_items=[], final_output=None)

    return _make


def _png_bytes(width: int, height: int) -> bytes:
    """最小的合法 PNG header：簽章 + IHDR chunk（含正確 CRC）。"""
    ihdr_body = struct.pack(">II", width, height) + bytes(
        [8, 2, 0, 0, 0]  # bit depth, color type(truecolor), compression, filter, interlace
    )
    chunk = b"IHDR" + ihdr_body
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(ihdr_body))
        + chunk
        + struct.pack(">I", zlib.crc32(chunk))
    )


def _gif_bytes(width: int, height: int) -> bytes:
    """最小的合法 GIF header：GIF89a 簽章 + 邏輯螢幕描述子（尺寸為 little-endian）。"""
    return b"GIF89a" + struct.pack("<HH", width, height) + bytes([0x00, 0x00, 0x00])


def _jpeg_bytes(width: int, height: int) -> bytes:
    """最小的合法 JPEG header：SOI + APP0(JFIF) + SOF0（尺寸在 SOF0，高在前寬在後）+ EOI。"""
    app0_body = b"JFIF\x00" + bytes([1, 1, 0]) + struct.pack(">HH", 1, 1) + bytes([0, 0])
    app0 = b"\xff\xe0" + struct.pack(">H", len(app0_body) + 2) + app0_body

    sof0_body = bytes([8]) + struct.pack(">HH", height, width) + bytes([1]) + bytes([1, 0x11, 0])
    sof0 = b"\xff\xc0" + struct.pack(">H", len(sof0_body) + 2) + sof0_body

    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def _webp_bytes(width: int, height: int) -> bytes:
    """最小的合法 WebP header：RIFF/WEBP 容器 + 有損 VP8 keyframe header。

    VP8 keyframe 的尺寸為 14 bits，故寫入前需與 0x3FFF 遮罩對齊。
    """
    vp8_payload = (
        bytes([0x9D, 0x01, 0x2A])  # keyframe start code
        + struct.pack("<HH", width & 0x3FFF, height & 0x3FFF)
    )
    vp8_chunk = b"VP8 " + struct.pack("<I", len(vp8_payload) + 3) + bytes([0, 0, 0]) + vp8_payload
    body = b"WEBP" + vp8_chunk
    return b"RIFF" + struct.pack("<I", len(body)) + body


@pytest.fixture
def tiny_images() -> Dict[str, bytes]:
    """四種格式的最小合法影像 header，尺寸皆為 TINY_IMAGE_WIDTH × TINY_IMAGE_HEIGHT。"""
    return {
        "jpeg": _jpeg_bytes(TINY_IMAGE_WIDTH, TINY_IMAGE_HEIGHT),
        "png": _png_bytes(TINY_IMAGE_WIDTH, TINY_IMAGE_HEIGHT),
        "webp": _webp_bytes(TINY_IMAGE_WIDTH, TINY_IMAGE_HEIGHT),
        "gif": _gif_bytes(TINY_IMAGE_WIDTH, TINY_IMAGE_HEIGHT),
    }
