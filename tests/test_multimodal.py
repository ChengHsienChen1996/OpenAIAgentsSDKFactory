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
import json
import logging
import mimetypes
import time

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
# 估算誤差量測
# --------------------------------------------------------------------------- #

class TraceCapture(logging.Handler):
    """自 rate.guard logger 擷取 trace 事件。

    不能改寫 limit_runner.trace —— 它在 limits_guard_multi 套用裝飾器時就被綁定，
    事後替換模組屬性無效。
    """

    def __init__(self):
        super().__init__()
        self.events: List[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if not message.startswith("estimate_ready "):
            return
        try:
            self.events.append(json.loads(message.split(" ", 1)[1]))
        except (ValueError, IndexError):
            pass

    def pop_latest(self) -> Optional[dict]:
        return self.events[-1] if self.events else None


def extract_usage(result) -> Tuple[Optional[int], Optional[int]]:
    """自 RunResult 取出實際的 (input_tokens, total_tokens)。

    Returns:
        兩者皆為供應商回報值；未回報時回傳 (None, None)。
        Ollama 不回報 usage，此時無法計算誤差。
    """
    input_tokens = 0
    total_tokens = 0
    reported = False

    for raw in getattr(result, "raw_responses", None) or []:
        usage = getattr(raw, "usage", None)
        if usage is None:
            continue
        if getattr(usage, "total_tokens", 0):
            reported = True
        input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        total_tokens += int(getattr(usage, "total_tokens", 0) or 0)

    return (input_tokens, total_tokens) if reported else (None, None)


@dataclass
class MeasurementRow:
    """單次呼叫的量測結果。

    兩種口徑刻意分開，混用會讓統計失去意義：

    - **估算口徑**：``estimated_input``（= 輸入估算 + system prompt）對比 ``actual_input``。
      兩者都只涵蓋輸入側，是衡量「估算準不準」的唯一公平比較。
    - **預扣口徑**：``reserved`` 對比 ``actual_total``。``reserved`` 另含
      ``output_buffer × max_tokens``、``safety_pad``、``per_round_pad``，
      本來就會系統性高於實際用量，只能用來衡量「配額佔用多少」，不能當估算誤差。
    """

    label: str
    reserved: int
    estimated_input: int
    text_tok: int
    image_tok: int
    other_tok: int
    image_count: int
    actual_input: Optional[int]
    actual_total: Optional[int]

    @property
    def estimate_ratio(self) -> Optional[float]:
        """估算口徑的高估倍率；> 1 為高估，< 1 為低估。"""
        if not self.actual_input:
            return None
        return self.estimated_input / self.actual_input

    @property
    def reserve_ratio(self) -> Optional[float]:
        if not self.actual_total:
            return None
        return self.reserved / self.actual_total


def print_measurement_report(rows: List[MeasurementRow]) -> None:
    """輸出誤差統計。兩種口徑分開列出並標明含義。"""
    print("\n" + "=" * 78)
    print("估算誤差統計")
    print("=" * 78)

    header = f"{'標的':<22}{'reserved':>9}{'估算輸入':>9}{'實際輸入':>9}{'估算倍率':>10}"
    print(header)
    print("-" * 78)
    for row in rows:
        ratio = f"{row.estimate_ratio:.2f}x" if row.estimate_ratio else "n/a"
        actual = row.actual_input if row.actual_input is not None else "n/a"
        print(f"{row.label:<22}{row.reserved:>9,}{row.estimated_input:>9,}{str(actual):>9}{ratio:>10}")

    measurable = [r for r in rows if r.estimate_ratio is not None]
    print("-" * 78)

    if not measurable:
        print("⚠️  供應商未回報 usage，無法計算估算誤差。")
        print("    （已知 Ollama 的 OpenAI-compatible endpoint 不回報 usage）")
        print("    此情況下只能確認預扣量的量級，無法驗證估算準確度。")
        return

    ratios = [r.estimate_ratio for r in measurable]
    underestimated = [r for r in measurable if r.estimate_ratio < 1.0]

    print("【估算口徑】輸入估算 + system prompt  vs  供應商回報的 input_tokens")
    print(f"    平均高估倍率：{sum(ratios) / len(ratios):.2f}x")
    print(f"    最大高估倍率：{max(ratios):.2f}x")
    print(f"    最小倍率　　：{min(ratios):.2f}x")
    print(f"    出現低估？　：{'⚠️ 是（會導致 429）' if underestimated else '否'}")
    if underestimated:
        for row in underestimated:
            print(f"        - {row.label}: 估 {row.estimated_input:,} < 實際 {row.actual_input:,}")

    reserve_ratios = [r.reserve_ratio for r in rows if r.reserve_ratio is not None]
    if reserve_ratios:
        print()
        print("【預扣口徑】reserved  vs  供應商回報的 total_tokens")
        print("    註：reserved 另含 output_buffer × max_tokens 與各項 pad，")
        print("        本來就會高於實際用量，不代表估算誤差。")
        print(f"    平均倍率：{sum(reserve_ratios) / len(reserve_ratios):.2f}x")


async def measure_one(runner, capture: TraceCapture, model_input, label: str) -> MeasurementRow:
    """跑一次呼叫並回傳量測結果。"""
    result = await runner.run(input_=model_input)

    fields = capture.pop_latest() or {}
    actual_input, actual_total = extract_usage(result)

    return MeasurementRow(
        label=label,
        reserved=fields.get("reserved_tokens", 0),
        estimated_input=fields.get("user_tok", 0) + fields.get("sys_tok", 0),
        text_tok=fields.get("text_tok", 0),
        image_tok=fields.get("image_tok", 0),
        other_tok=fields.get("other_tok", 0),
        image_count=fields.get("image_count", 0),
        actual_input=actual_input,
        actual_total=actual_total,
    )


def install_trace_capture() -> TraceCapture:
    """接管 rate.guard logger，回傳擷取器。"""
    capture = TraceCapture()
    logger = logging.getLogger("rate.guard")
    logger.handlers = [capture]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return capture


# --------------------------------------------------------------------------- #
# 手動執行入口（直接看辨識結果用，非 pytest 流程）
# --------------------------------------------------------------------------- #

async def _main(args: argparse.Namespace) -> None:
    yaml_path = Path(args.yaml) if args.yaml else YAML_SETTINGS_FILE
    factory = AgentFactory.create_factory_from_yaml(yaml_path)
    runner = LimitAgentRunner(agent=factory.get_agent_by_name(args.agent))

    if args.image:
        image = Path(args.image)
        images = [image if image.is_absolute() else PROJECT_ROOT / image]
    else:
        images = collect_images(IMAGE_DIR, args.limit)

    capture = install_trace_capture() if args.measure else None
    rows: List[MeasurementRow] = []

    for image_path in images:
        data_url = image_to_data_url(image_path, args.max_side)
        model_input = build_input(data_url, args.prompt)
        started = time.monotonic()

        if args.measure:
            row = await measure_one(runner, capture, model_input, image_path.name)
            rows.append(row)
            print(
                f"  {image_path.name}：reserved={row.reserved:,} "
                f"(text={row.text_tok} image={row.image_tok:,} other={row.other_tok}) "
                f"實際輸入={row.actual_input if row.actual_input is not None else 'n/a'} "
                f"耗時 {time.monotonic() - started:.1f}s",
                flush=True,
            )
        else:
            result = await runner.run(input_=model_input)
            print(f"\n--- {image_path.name}（耗時 {time.monotonic() - started:.1f}s）---")
            print(result.final_output)

    if args.measure:
        print_measurement_report(rows)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="多模態手動測試與估算誤差量測")
    parser.add_argument("--image", help="只測試單一圖片（相對於專案根目錄或絕對路徑）")
    parser.add_argument("--limit", type=int, default=None, help="最多測試幾張圖片")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help=f"文字提示，預設 {DEFAULT_PROMPT!r}")
    parser.add_argument(
        "--max-side",
        type=int,
        default=0,
        help="縮圖長邊上限（需安裝 Pillow），0 表示不縮圖（預設）",
    )
    parser.add_argument("--yaml", help=f"agent 設定 YAML，預設 {YAML_SETTINGS_FILE.name}")
    parser.add_argument("--agent", default=AGENT_NAME, help=f"agent 名稱，預設 {AGENT_NAME}")
    parser.add_argument(
        "--measure",
        action="store_true",
        help="輸出估算誤差統計，而非辨識結果",
    )
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(_main(_parse_args()))
