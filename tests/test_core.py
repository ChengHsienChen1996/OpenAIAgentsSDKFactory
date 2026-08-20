"""AgentFactory 的單元測試。

由原 tests/test_basic.py 拆分而來：本檔負責 AgentFactory，
YAML 載入與 Pydantic 驗證的部分移至 tests/test_config_loader.py。
"""
import os

import pytest
from agents import Agent

from agent_factory.core import AgentFactory
from agent_factory.limit_runner import LimitAgentRunner


def test_create_factory_from_yaml_returns_factory(sample_yaml_path):
    """能從 YAML 路徑初始化 AgentFactory。"""
    factory = AgentFactory.create_factory_from_yaml(sample_yaml_path)

    assert isinstance(factory, AgentFactory)


def test_get_agent_by_name_returns_configured_agent(sample_factory):
    """依 YAML 的 name 欄位取得 Agent，且回傳的是設定中那一個。"""
    agent = sample_factory.get_agent_by_name("SampleAgent")

    assert isinstance(agent, Agent)
    assert agent.name == "SampleAgent"


def test_get_agent_by_name_uses_static_instruction_file(sample_factory):
    """dynamic_prompt=false 時，instructions 應為 instruction_file_path 的檔案內容。"""
    agent = sample_factory.get_agent_by_name("SampleAgent")

    assert isinstance(agent.instructions, str)
    assert "單元測試用的助理" in agent.instructions


def test_get_agent_by_name_unknown_raises_key_error(sample_factory):
    """取不到對應名稱的 agent 時拋出 KeyError，訊息帶上該名稱。"""
    with pytest.raises(KeyError, match="NotExist"):
        sample_factory.get_agent_by_name("NotExist")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_agent_against_real_api():
    """實際呼叫 API（需 YAML_SETTINGS_FILE 與有效金鑰，預設不執行）。"""
    yaml_path = os.getenv("YAML_SETTINGS_FILE")
    if not yaml_path:
        pytest.skip("未設定 YAML_SETTINGS_FILE")

    factory = AgentFactory.create_factory_from_yaml(yaml_path)
    agent = factory.get_agent_by_name(os.getenv("INTEGRATION_AGENT_NAME", "GemmaTestAgent"))

    runner = LimitAgentRunner(agent=agent)
    result = await runner.run(input_="請用一句話介紹你自己")

    assert result.final_output
