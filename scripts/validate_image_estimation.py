#!/usr/bin/env python
"""以真實供應商回報的 token 數驗證影像估算公式（Phase 4 Task 4.2）。

作法：對每個模型先送一次「純文字」呼叫取得基準輸入 token 數，再送各尺寸的影像呼叫。
兩者相減即為供應商實際計算的影像 token，可與 image_tokens.py 的估算值直接比對。
這樣能隔離 system prompt 與文字提示的影響，避免把文字誤差算進影像公式的帳上。

刻意使用合成的已知尺寸影像，而非 imgs/ 內的相片 —— 公式驗證需要精確控制寬高。

用法（需要 OPENAI_API_KEY）：

    uv run python scripts/validate_image_estimation.py
    uv run python scripts/validate_image_estimation.py --agent PatchBasedAgent
    uv run python scripts/validate_image_estimation.py --sizes 1024x1024

成本：預設 2 個模型 × (1 次純文字 + 4 張影像) = 10 次呼叫，皆為 mini 級模型，
輸出上限 64 tokens，實測總花費在 0.05 美元以內。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import struct
import sys
import zlib

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv()

from agents import set_tracing_disabled

set_tracing_disabled(True)

from agent_factory.core import AgentFactory
from agent_factory.limit_runner import LimitAgentRunner
from agent_factory.rate_limiter.image_tokens import estimate_image_tokens, read_image_size
from agent_factory.rate_limiter.token_counter import count_tokens

CLOUD_YAML = PROJECT_ROOT / "tests" / "cloud_vision_agents_setup.yaml"
DEFAULT_AGENTS = ["TileBasedAgent", "PatchBasedAgent"]
DEFAULT_SIZES = [(256, 256), (512, 512), (1024, 1024), (1024, 1536)]
PROBE_PROMPT = "What colour dominates?"
# 基準呼叫用的填充文字，取代影像 item；長度計入後扣除，故取單一 token 的短字串。
BASELINE_FILLER = "x"

# 驗收基準（見 .agent/plans/phase-4-validation-and-docs.md Task 4.2 要求 4）
MAX_ACCEPTABLE_RATIO = 3.0

FILLER_TOKENS = 1  # BASELINE_FILLER 的 token 數


def png_of_size(width: int, height: int) -> bytes:
    """產生指定尺寸、完整可解碼的 PNG。圖樣為規律漸層，壓縮後體積小。"""
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
        + chunk(b"IDAT", zlib.compress(raw, level=6))
        + chunk(b"IEND", b"")
    )


def data_url(payload: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(payload).decode("utf-8")


class TraceCapture(logging.Handler):
    """擷取 estimate_ready 事件。limit_runner.trace 在裝飾時已綁定，無法事後替換。"""

    def __init__(self):
        super().__init__()
        self.latest: dict = {}

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith("estimate_ready "):
            try:
                self.latest = json.loads(message.split(" ", 1)[1])
            except (ValueError, IndexError):
                pass


def actual_input_tokens(result) -> Optional[int]:
    total = 0
    reported = False
    for raw in getattr(result, "raw_responses", None) or []:
        usage = getattr(raw, "usage", None)
        if usage is None:
            continue
        value = int(getattr(usage, "input_tokens", 0) or 0)
        if value:
            reported = True
        total += value
    return total if reported else None


@dataclass
class Row:
    model: str
    size: Tuple[int, int]
    estimated_image: int
    actual_image: int

    @property
    def ratio(self) -> float:
        return self.estimated_image / self.actual_image if self.actual_image else float("inf")


async def measure_agent(agent_name: str, sizes: List[Tuple[int, int]], capture: TraceCapture) -> List[Row]:
    factory = AgentFactory.create_factory_from_yaml(CLOUD_YAML)
    agent = factory.get_agent_by_name(agent_name)
    model_name = agent.model.model
    runner = LimitAgentRunner(agent=agent)

    print(f"\n### {agent_name}（{model_name}）")

    # 基準呼叫必須與影像呼叫「訊息結構完全相同」，只把影像 item 換成一個極短的文字 item。
    # 否則兩者的 message overhead 不同，差額會被誤算進影像成本（實測顯示固定偏差 3~4 tokens，
    # 足以讓本來精準的估算看起來像低估）。
    baseline_messages = [
        {"role": "user", "content": [{"type": "input_text", "text": BASELINE_FILLER}]},
        {"role": "user", "content": PROBE_PROMPT},
    ]
    baseline_result = await runner.run(input_=baseline_messages)
    baseline = actual_input_tokens(baseline_result)
    if baseline is None:
        print("  ⚠️ 供應商未回報 usage，無法驗證")
        return []
    print(f"  結構對齊基準：實際輸入 {baseline} tokens（影像 item 以 {BASELINE_FILLER!r} 取代）")

    rows: List[Row] = []
    for width, height in sizes:
        url = data_url(png_of_size(width, height))
        parsed = read_image_size(url)
        estimated_image = estimate_image_tokens(url, model_name, "auto")

        messages = [
            {"role": "user", "content": [{"type": "input_image", "detail": "auto", "image_url": url}]},
            {"role": "user", "content": PROBE_PROMPT},
        ]
        result = await runner.run(input_=messages)
        actual_total_input = actual_input_tokens(result)

        # 影像的實際成本 = 含影像的輸入 token − 結構對齊的基準 + 填充文字本身的 token 數。
        # 兩次呼叫的訊息結構相同，overhead 因而相互抵消。
        actual_image = actual_total_input - baseline + FILLER_TOKENS

        rows.append(Row(model_name, (width, height), estimated_image, actual_image))
        print(
            f"  {width}x{height}：解析尺寸={parsed} 估算影像={estimated_image:,} "
            f"實際影像≈{actual_image:,} 倍率={estimated_image / actual_image:.2f}x"
            if actual_image else
            f"  {width}x{height}：解析尺寸={parsed} 估算影像={estimated_image:,} 實際影像≈{actual_image}",
            flush=True,
        )

    return rows


def print_report(rows: List[Row]) -> int:
    print("\n" + "=" * 78)
    print("影像估算公式驗證結果（估算影像 token vs 供應商實際計費）")
    print("=" * 78)
    print(f"{'模型':<16}{'尺寸':>12}{'估算':>10}{'實際':>10}{'倍率':>10}{'判定':>12}")
    print("-" * 78)

    underestimates: List[Row] = []
    over_threshold: List[Row] = []

    for row in rows:
        if row.ratio < 1.0:
            verdict = "低估 ⚠️"
            underestimates.append(row)
        elif row.ratio > MAX_ACCEPTABLE_RATIO:
            verdict = "高估超標 ⚠️"
            over_threshold.append(row)
        else:
            verdict = "通過"
        size = f"{row.size[0]}x{row.size[1]}"
        print(f"{row.model:<16}{size:>12}{row.estimated_image:>10,}{row.actual_image:>10,}"
              f"{row.ratio:>9.2f}x{verdict:>12}")

    print("-" * 78)
    ratios = [r.ratio for r in rows]
    if ratios:
        print(f"平均倍率：{sum(ratios) / len(ratios):.2f}x　最大：{max(ratios):.2f}x　最小：{min(ratios):.2f}x")
    print(f"驗收基準：不得低估（倍率 ≥ 1.0），且高估不超過 {MAX_ACCEPTABLE_RATIO}x")
    print()

    if underestimates:
        print("❌ 出現低估 —— 會導致撞供應商 429：")
        for row in underestimates:
            print(f"    {row.model} {row.size[0]}x{row.size[1]}: 估 {row.estimated_image:,} < 實際 {row.actual_image:,}")
    if over_threshold:
        print(f"⚠️  高估超過 {MAX_ACCEPTABLE_RATIO}x：")
        for row in over_threshold:
            print(f"    {row.model} {row.size[0]}x{row.size[1]}: {row.ratio:.2f}x")
    if not underestimates and not over_threshold:
        print("✅ 全部通過：無低估，高估皆在基準內。")

    return 1 if underestimates else 0


async def main(args: argparse.Namespace) -> int:
    capture = TraceCapture()
    logger = logging.getLogger("rate.guard")
    logger.handlers = [capture]
    logger.setLevel(logging.INFO)
    logger.propagate = False

    sizes = DEFAULT_SIZES
    if args.sizes:
        sizes = [tuple(int(v) for v in s.split("x")) for s in args.sizes]

    agents = [args.agent] if args.agent else DEFAULT_AGENTS

    rows: List[Row] = []
    for agent_name in agents:
        rows.extend(await measure_agent(agent_name, sizes, capture))

    return print_report(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="影像估算公式的實測驗證")
    parser.add_argument("--agent", help=f"只測單一 agent，預設兩者皆測：{', '.join(DEFAULT_AGENTS)}")
    parser.add_argument("--sizes", nargs="*", help="影像尺寸，格式 WxH，例如 1024x1024")
    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse_args())))
