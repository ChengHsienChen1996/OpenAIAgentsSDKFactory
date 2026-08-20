"""輸入 token 計數與估算。

``count_tokens`` 是單純的文字計數，簽名維持不變（system prompt 計數與外部使用仍依賴它）。

``estimate_input_tokens`` 走訪結構化的 ``input_``，把影像交給 image_tokens 換算，
絕不對 base64 影像字串做文字計數 —— 這是本模組存在的理由：一張 2.7 MB 相片若當文字計數
會估到約 244 萬 token，且光是編碼就要 40 秒。

走訪採「明確白名單」而非「掃描所有字串欄位」：SDK 的 ``computer_call_output.output``
是截圖 dict、``image_generation_call.result`` 是 base64 影像，通用掃描會重蹈覆轍。
未列於白名單的 item 一律計入保守常數並標記 has_unknown_items（見 docs/architecture.md 原則 3）。
"""
from __future__ import annotations

import logging

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import tiktoken

from tiktoken.core import Encoding

from .image_tokens import estimate_image_tokens

logger = logging.getLogger("rate.guard")

# 每則 message 的固定額外成本（role、分隔符等）。
PER_MESSAGE_OVERHEAD = 4

# 無法識別的 item 一律以此值計入。寧可高估：略過等同低估，會撞供應商 429。
UNKNOWN_ITEM_TOKENS = 1000

DEFAULT_IMAGE_DETAIL = "auto"

# content item 中，型別 → 取哪個欄位做文字計數。
_TEXT_CONTENT_FIELDS: Dict[str, str] = {
    "input_text": "text",
    "output_text": "text",
    "refusal": "refusal",
}

IMAGE_CONTENT_TYPE = "input_image"

# 非 message item 中，型別 → 可安全做文字計數的欄位（必須確定是純字串）。
# 刻意不含 computer_call_output（output 為截圖 dict）與 image_generation_call（result 為 base64 影像）。
_TEXT_ITEM_FIELDS: Dict[str, Tuple[str, ...]] = {
    "function_call": ("name", "arguments"),
    "function_call_output": ("output",),
    "local_shell_call_output": ("output",),
    "custom_tool_call": ("name", "input"),
    "custom_tool_call_output": ("output",),
    "mcp_call": ("name", "arguments", "output"),
    "mcp_approval_request": ("name", "arguments"),
    "code_interpreter_call": ("code",),
}

MESSAGE_ITEM_TYPE = "message"
REASONING_ITEM_TYPE = "reasoning"


def _get_encoder(model=None) -> Optional[Encoding]:
    try:
        if model:
            try: return tiktoken.encoding_for_model(model)
            except Exception: pass
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def count_tokens(text: str, model: str | None = None) -> int:
    enc = _get_encoder(model)
    return len(enc.encode(text or "")) if enc else max(1, (len(text or "") + 3)//4)


@dataclass
class InputTokenEstimate:
    """輸入 token 的分項估算結果。

    分項而非單一總數，是因為影像估算出錯時，只看總數無法分辨問題出在文字還是影像。
    這些欄位會進入 trace，是線上除錯的唯一依據。
    """

    text_tokens: int = 0
    image_tokens: int = 0
    other_tokens: int = 0
    total: int = 0
    image_count: int = 0
    has_unknown_items: bool = False

    def _add_text(self, tokens: int) -> None:
        self.text_tokens += tokens
        self.total += tokens

    def _add_image(self, tokens: int) -> None:
        self.image_tokens += tokens
        self.total += tokens
        self.image_count += 1

    def _add_other(self, tokens: int, *, unknown: bool = False) -> None:
        self.other_tokens += tokens
        self.total += tokens
        if unknown:
            self.has_unknown_items = True


def _estimate_content_item(
    item: Any,
    model_name: str,
    estimate: InputTokenEstimate,
) -> None:
    """處理 message.content 陣列中的單一 item。"""
    if not isinstance(item, dict):
        logger.warning("malformed_content_item type=%s", type(item).__name__)
        estimate._add_other(UNKNOWN_ITEM_TOKENS, unknown=True)
        return

    item_type = item.get("type")

    if item_type == IMAGE_CONTENT_TYPE:
        image_url = item.get("image_url") or item.get("file_id") or ""
        detail = item.get("detail") or DEFAULT_IMAGE_DETAIL
        estimate._add_image(estimate_image_tokens(image_url, model_name, detail))
        return

    text_field = _TEXT_CONTENT_FIELDS.get(item_type) if isinstance(item_type, str) else None
    if text_field is not None:
        estimate._add_text(count_tokens(item.get(text_field) or "", model_name))
        return

    # 未知型別。即使帶有 image_url 也不退回文字計數 —— 那正是本模組要避免的行為。
    logger.warning("unknown_content_item type=%r has_image_url=%s", item_type, "image_url" in item)
    estimate._add_other(UNKNOWN_ITEM_TOKENS, unknown=True)


def _estimate_message(item: Dict[str, Any], model_name: str, estimate: InputTokenEstimate) -> None:
    """處理帶 role/content 的 message item。"""
    estimate._add_other(PER_MESSAGE_OVERHEAD)

    content = item.get("content")

    if isinstance(content, str):
        estimate._add_text(count_tokens(content, model_name))
        return

    if isinstance(content, (list, tuple)):
        for content_item in content:
            _estimate_content_item(content_item, model_name, estimate)
        return

    # content 缺失或型別不預期：計入保守常數，不拋例外。
    logger.warning("malformed_message content_type=%s", type(content).__name__)
    estimate._add_other(UNKNOWN_ITEM_TOKENS, unknown=True)


def _estimate_reasoning(item: Dict[str, Any], model_name: str, estimate: InputTokenEstimate) -> None:
    """reasoning item：summary 與 content 皆為 [{"text": ...}] 形式。"""
    for field in ("summary", "content"):
        entries = item.get(field)
        if not isinstance(entries, (list, tuple)):
            continue
        for entry in entries:
            if isinstance(entry, dict):
                estimate._add_text(count_tokens(entry.get("text") or "", model_name))


def _estimate_item(item: Any, model_name: str, estimate: InputTokenEstimate) -> None:
    """處理 input_ 陣列中的單一 item。"""
    if not isinstance(item, dict):
        logger.warning("malformed_input_item type=%s", type(item).__name__)
        estimate._add_other(UNKNOWN_ITEM_TOKENS, unknown=True)
        return

    item_type = item.get("type")

    # message 的判定以 role 為準：EasyInputMessageParam 的 type 欄位是選填的。
    if "role" in item or item_type == MESSAGE_ITEM_TYPE:
        _estimate_message(item, model_name, estimate)
        return

    if item_type == REASONING_ITEM_TYPE:
        _estimate_reasoning(item, model_name, estimate)
        return

    text_fields = _TEXT_ITEM_FIELDS.get(item_type) if isinstance(item_type, str) else None
    if text_fields is not None:
        for field in text_fields:
            value = item.get(field)
            if isinstance(value, str):
                estimate._add_text(count_tokens(value, model_name))
        return

    # 其餘型別（computer_call_output、image_generation_call、item_reference 等）
    # 可能夾帶影像或指向看不到的內容，一律計入保守常數。
    logger.warning("unknown_input_item type=%r", item_type)
    estimate._add_other(UNKNOWN_ITEM_TOKENS, unknown=True)


def estimate_input_tokens(input_: Any, model_name: str) -> InputTokenEstimate:
    """估算 ``LimitAgentRunner.run(input_=...)`` 的輸入 token 數。

    Args:
        input_: 字串，或 openai-agents SDK 的 message／item 陣列。
        model_name: 模型名稱，決定影像換算公式與 tiktoken 編碼。

    Returns:
        分項的 InputTokenEstimate。任何畸形結構都不會拋例外 ——
        估算失敗不該讓請求掛掉，改以保守常數計入並標記 has_unknown_items。
    """
    estimate = InputTokenEstimate()

    if input_ is None:
        return estimate

    if isinstance(input_, str):
        estimate._add_text(count_tokens(input_, model_name))
        return estimate

    if isinstance(input_, (list, tuple)):
        for item in input_:
            _estimate_item(item, model_name, estimate)
        return estimate

    logger.warning("unsupported_input_type type=%s", type(input_).__name__)
    estimate._add_other(UNKNOWN_ITEM_TOKENS, unknown=True)
    return estimate
