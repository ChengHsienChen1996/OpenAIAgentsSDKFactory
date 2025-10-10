import logging
import json
import sys

from typing import Optional, List
from pydantic import BaseModel

from agents import Agent, Runner, RunResult
from agents.run_context import TContext
from agents.items import (ToolCallItem,ToolCallOutputItem, MCPListToolsItem,
                          MCPApprovalRequestItem, MCPApprovalResponseItem,)

from .rate_limiter.wrappers import limits_guard_multi
from .rate_limiter.limits_parameters import umbrella, registry


logger = logging.getLogger("rate.guard")
logger.setLevel(logging.INFO)

from agents.items import TResponseInputItem


class ToolTrace(BaseModel):
    tool_trace: List[TResponseInputItem]

    def get_tool_trace(self) -> List[str]:
        return [self._serialize_item(item) for item in self.tool_trace]

    @staticmethod
    def _serialize_item(item: TResponseInputItem) -> str:
        """Serialize an item to JSON string. Can be overridden by subclasses."""
        return json.dumps(item, separators=(",", ":"))


# 確保有一個輸出到 stdout 的 handler
if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(h)


def trace(event: str, fields: dict):
    logger.info("%s %s", event, json.dumps(fields, ensure_ascii=False))


class LimitAgentRunner:
    def __init__(self, agent: Agent):
        self.agent = agent

    @limits_guard_multi(
        registry=registry,
        umbrella=umbrella,
        input_arg="input",
        context_arg="context",
        max_output_tokens=2048,
        output_buffer_mult=1.2,
        safety_pad_tokens=64,
        per_round_pad=300,
        trace=trace,               # ★ 開啟追蹤
        warn_after_s=15.0,         # ★ 超過15秒未取得配額 → 開始心跳
        heartbeat_every_s=15.0,    # ★ 每15秒打一條 still_waiting
    )
    async def run(self, input_: str | List[TResponseInputItem], context: Optional[TContext] = None, **kwargs):
        return await Runner.run(starting_agent=self.agent, input=input_, context=context, **kwargs)


    async def extract_tool_usage(self, run_result: RunResult) -> ToolTrace:
        tool_usage = [item.to_input_item() for item in run_result.new_items if self.check_if_tool_usage_object(item)]
        return ToolTrace(tool_trace=tool_usage)


    @staticmethod
    def check_if_tool_usage_object(_obj) -> bool:
        return (isinstance(_obj, ToolCallOutputItem) & isinstance(_obj, ToolCallItem) & isinstance(_obj, MCPApprovalRequestItem)
                & isinstance(_obj, MCPApprovalResponseItem) & isinstance(_obj, MCPListToolsItem))
