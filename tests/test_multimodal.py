"""多模態（影像輸入）測試：以 Docker 版 Ollama 的 glm-ocr-optimized:latest 辨識 ./imgs 內的圖片。

原生 openai-agents SDK 的寫法是自行 `Agent(...)` + `Runner.run(...)`；
本 repo 改為由 YAML 宣告 agent（AgentFactory），並以 LimitAgentRunner 帶速率限制執行。

實際呼叫 Ollama 的案例標記為 integration，預設不執行。手動執行方式：

    uv run pytest -m integration tests/test_multimodal.py -s      # 走 pytest
    uv run python tests/test_multimodal.py --limit 1              # 直接執行、看辨識結果
"""
import argparse
import asyncio
import base64
import io
import mimetypes
import time

from pathlib import Path
from typing import Dict, List, Optional

import pytest

from agents import set_tracing_disabled
from agents.items import TResponseInputItem

from agent_factory.core import AgentFactory
from agent_factory.limit_runner import LimitAgentRunner

# 本測試打的是本地 Ollama，沒有 OpenAI 金鑰可上傳 trace，直接關閉避免噪音
set_tracing_disabled(True)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
YAML_SETTINGS_FILE = PROJECT_ROOT / "tests" / "multimodal_agents_setup.yaml"
IMAGE_DIR = PROJECT_ROOT / "imgs"
AGENT_NAME = "OllamaOCRAgent"
DEFAULT_PROMPT = "Table Recognition:"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}


def collect_images(image_dir: Path, limit: Optional[int] = None) -> List[Path]:
    """取得資料夾內所有圖片路徑（依檔名排序）。"""
    images = sorted(p for p in image_dir.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
    if not images:
        raise FileNotFoundError(f"{image_dir} 內找不到任何圖片")
    return images[:limit] if limit else images


def image_to_data_url(image_path: Path, max_side: int = 0) -> str:
    """讀取圖片並轉成 data URL。

    Args:
        image_path: 圖片路徑。
        max_side: 長邊上限（像素）；> 0 且環境有安裝 Pillow 時會先等比縮圖。
            手機直出相片動輒 2~3 MB，base64 後會讓 count_tokens 的預扣量暴增，
            縮圖可大幅縮短等待與傳輸時間。沒有 Pillow 時自動略過。

    Returns:
        ``data:<mime>;base64,<payload>`` 字串。
    """
    raw = image_path.read_bytes()
    mime = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"

    if max_side > 0:
        raw, mime = _downscale(raw, mime, max_side)

    return f"data:{mime};base64,{base64.b64encode(raw).decode('utf-8')}"


def _downscale(raw: bytes, mime: str, max_side: int) -> tuple[bytes, str]:
    """等比縮圖至長邊不超過 max_side；未安裝 Pillow 或不需縮圖時原樣回傳。"""
    try:
        from PIL import Image
    except ImportError:
        return raw, mime

    with Image.open(io.BytesIO(raw)) as im:
        if max(im.size) <= max_side:
            return raw, mime
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=90)

    return buf.getvalue(), "image/jpeg"


def build_input(data_url: str, prompt: str) -> List[TResponseInputItem]:
    """組出 Runner 用的多模態 input（一則影像訊息 + 一則文字訊息）。"""
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_image",
                    "detail": "auto",
                    "image_url": data_url,
                }
            ],
        },
        {
            "role": "user",
            "content": prompt,
        },
    ]


# --------------------------------------------------------------------------- #
# 不需外部服務的測試
# --------------------------------------------------------------------------- #

def test_multimodal_yaml_builds_agent():
    """多模態 YAML 能建出 Agent（僅建立 client，不發出任何請求）。"""
    factory = AgentFactory.create_factory_from_yaml(YAML_SETTINGS_FILE)
    agent = factory.get_agent_by_name(AGENT_NAME)

    assert agent.name == AGENT_NAME
    assert agent.model.model == "glm-ocr-optimized:latest"


def test_image_to_data_url_produces_base64_payload(tmp_path, tiny_images: Dict[str, bytes]):
    """影像會被轉成可解碼的 base64 data URL，且 mime 依副檔名判定。"""
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(tiny_images["png"])

    data_url = image_to_data_url(image_path)

    assert data_url.startswith("data:image/png;base64,")
    payload = data_url.split(",", 1)[1]
    assert base64.b64decode(payload) == tiny_images["png"]


def test_build_input_wraps_image_and_prompt(tmp_path, tiny_images: Dict[str, bytes]):
    """多模態 input 應為「影像 message + 文字 message」兩則。"""
    image_path = tmp_path / "sample.jpeg"
    image_path.write_bytes(tiny_images["jpeg"])

    model_input = build_input(image_to_data_url(image_path), DEFAULT_PROMPT)

    assert len(model_input) == 2
    image_item = model_input[0]["content"][0]
    assert image_item["type"] == "input_image"
    assert image_item["image_url"].startswith("data:image/jpeg;base64,")
    assert model_input[1]["content"] == DEFAULT_PROMPT


# --------------------------------------------------------------------------- #
# 需要本地 Ollama 的測試
# --------------------------------------------------------------------------- #

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(600)
async def test_run_ocr_against_local_ollama():
    """實際呼叫本地 Ollama 辨識 ./imgs 的第一張圖片。"""
    if not IMAGE_DIR.is_dir():
        pytest.skip(f"找不到測試影像資料夾 {IMAGE_DIR}")

    factory = AgentFactory.create_factory_from_yaml(YAML_SETTINGS_FILE)
    agent = factory.get_agent_by_name(AGENT_NAME)
    image_path = collect_images(IMAGE_DIR, limit=1)[0]

    runner = LimitAgentRunner(agent=agent)
    result = await runner.run(input_=build_input(image_to_data_url(image_path), DEFAULT_PROMPT))

    assert result.final_output


# --------------------------------------------------------------------------- #
# 手動執行入口（直接看辨識結果用，非 pytest 流程）
# --------------------------------------------------------------------------- #

async def _main(args: argparse.Namespace) -> None:
    factory = AgentFactory.create_factory_from_yaml(YAML_SETTINGS_FILE)
    agent = factory.get_agent_by_name(AGENT_NAME)
    runner = LimitAgentRunner(agent=agent)

    if args.image:
        image = Path(args.image)
        images = [image if image.is_absolute() else PROJECT_ROOT / image]
    else:
        images = collect_images(IMAGE_DIR, args.limit)

    for image_path in images:
        data_url = image_to_data_url(image_path, args.max_side)
        started = time.monotonic()
        result = await runner.run(input_=build_input(data_url, args.prompt))
        print(f"\n--- {image_path.name}（耗時 {time.monotonic() - started:.1f}s）---")
        print(result.final_output)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ollama 多模態（OCR）手動測試")
    parser.add_argument("--image", help="只測試單一圖片（相對於專案根目錄或絕對路徑）")
    parser.add_argument("--limit", type=int, default=None, help="最多測試幾張圖片")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help=f"文字提示，預設 {DEFAULT_PROMPT!r}")
    parser.add_argument(
        "--max-side",
        type=int,
        default=0,
        help="縮圖長邊上限（需安裝 Pillow），0 表示不縮圖（預設）",
    )
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(_main(_parse_args()))
