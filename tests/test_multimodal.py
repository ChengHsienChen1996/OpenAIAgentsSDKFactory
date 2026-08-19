# tests/test_multimodal.py
"""多模態（影像輸入）測試：以 Docker 版 Ollama 的 glm-ocr-optimized:latest 辨識 ./imgs 內的圖片。

原生 openai-agents SDK 的寫法是自行 `Agent(...)` + `Runner.run(...)`；
本 repo 改為由 YAML 宣告 agent（AgentFactory），並以 LimitAgentRunner 帶速率限制執行。

執行：
    uv run python test/test_multimodal.py
    uv run python test/test_multimodal.py --limit 1
    uv run python test/test_multimodal.py --image imgs/20260819_152629.jpg --prompt "OCR:"
"""
import argparse
import asyncio
import base64
import io
import mimetypes
import sys
import time

from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv()

from agents import set_tracing_disabled
from agents.items import TResponseInputItem

from src.agent_factory.core import AgentFactory
from src.agent_factory.limit_runner import LimitAgentRunner

# 本測試打的是本地 Ollama，沒有 OpenAI 金鑰可上傳 trace，直接關閉避免噪音
set_tracing_disabled(True)

YAML_SETTINGS_FILE = PROJECT_ROOT / "test" / "multimodal_agents_setup.yaml"
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


def test_factory_init() -> AgentFactory:
    """測試 AgentFactory 能從多模態 YAML 正確初始化"""
    factory = AgentFactory.create_factory_from_yaml(YAML_SETTINGS_FILE)
    assert factory is not None
    print(f"✅ AgentFactory 初始化成功：{YAML_SETTINGS_FILE.relative_to(PROJECT_ROOT)}")
    return factory


def test_get_agent(factory: AgentFactory):
    """測試能取得多模態 Agent 實例"""
    agent = factory.get_agent_by_name(AGENT_NAME)
    assert agent is not None
    assert agent.name == AGENT_NAME
    print(f"✅ 取得 Agent 成功：{agent.name}（model={agent.model.model}）")
    return agent


def test_collect_images(limit: Optional[int]) -> List[Path]:
    """測試能在 ./imgs 找到測試圖片"""
    images = collect_images(IMAGE_DIR, limit)
    print(f"✅ 找到 {len(images)} 張測試圖片：{', '.join(p.name for p in images)}")
    return images


def test_build_input(images: List[Path], prompt: str, max_side: int):
    """測試 data URL 與多模態 input 結構能正確組出"""
    data_url = image_to_data_url(images[0], max_side)
    assert data_url.startswith("data:image/")
    model_input = build_input(data_url, prompt)
    assert model_input[0]["content"][0]["type"] == "input_image"
    assert model_input[1]["content"] == prompt
    print(f"✅ 多模態 input 組裝成功（data URL 長度 {len(data_url):,} 字元）")


async def test_run_ocr(agent, image_path: Path, prompt: str, max_side: int):
    """測試實際呼叫 Ollama 進行影像辨識"""
    runner = LimitAgentRunner(agent=agent)
    data_url = image_to_data_url(image_path, max_side)

    started = time.monotonic()
    result = await runner.run(input_=build_input(data_url, prompt))
    elapsed = time.monotonic() - started

    assert result is not None
    print(f"\n--- {image_path.name}（耗時 {elapsed:.1f}s）---")
    print(result.final_output)
    return result


async def main(args: argparse.Namespace):
    print("=== 開始多模態測試 ===\n")

    # 不需要 API 的測試
    factory = test_factory_init()
    agent = test_get_agent(factory)

    if args.image:
        images = [Path(args.image) if Path(args.image).is_absolute() else PROJECT_ROOT / args.image]
        print(f"✅ 使用指定圖片：{images[0]}")
    else:
        images = test_collect_images(args.limit)

    test_build_input(images, args.prompt, args.max_side)

    # 需要 API 的測試（最後執行）
    print("\n--- Ollama 呼叫測試 ---")
    for image_path in images:
        await test_run_ocr(agent, image_path, args.prompt, args.max_side)

    print("\n=== 測試完成 ===")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ollama 多模態（OCR）測試")
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
    asyncio.run(main(parse_args()))
