# tests/test_basic.py
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

from src.agent_factory.core import AgentFactory
from src.agent_factory.limit_runner import LimitAgentRunner


def test_factory_init():
    """測試 AgentFactory 能正確從 YAML 初始化"""
    factory = AgentFactory.create_factory_from_yaml(os.getenv("YAML_SETTINGS_FILE"))
    assert factory is not None
    print("✅ AgentFactory 初始化成功")
    return factory


def test_get_agent(factory: AgentFactory):
    """測試能正確取得 Agent 實例"""
    agent = factory.get_agent_by_name("GemmaTestAgent")
    assert agent is not None
    assert agent.name == "GemmaTestAgent"
    print(f"✅ 取得 Agent 成功：{agent.name}")
    return agent


def test_get_agent_not_found(factory: AgentFactory):
    """測試取得不存在的 Agent 時拋出 KeyError"""
    try:
        factory.get_agent_by_name("NotExist")
        print("❌ 應該要拋出 KeyError")
    except KeyError:
        print("✅ KeyError 正確拋出")


def test_config_validation_fail():
    """測試 YAML 設定錯誤時能正確拋出 ValueError"""
    import tempfile, textwrap
    invalid_yaml = textwrap.dedent("""
        agents:
          test:
            - bad_agent:
                name: BadAgent
                model_instruction:
                  dynamic_prompt: true
                  instruction_file_path: some/path.md
                  # 故意漏掉 dynamic_module_path 和 model_context_path
                model_params:
                  model: gemma-4-31b-it
                  client:
                    api_key: test
                    base_url: https://example.com
    """)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(invalid_yaml)
        tmp_path = f.name

    try:
        AgentFactory.create_factory_from_yaml(tmp_path)
        print("❌ 應該要拋出 ValueError")
    except ValueError as e:
        print(f"✅ ValueError 正確拋出：{e}")
    finally:
        os.unlink(tmp_path)


async def test_run_agent(agent):
    """測試實際呼叫 API"""
    runner = LimitAgentRunner(agent=agent)
    result = await runner.run(input_="請用一句話介紹你自己")
    assert result is not None
    print(f"✅ Agent 回應成功")
    print(f"   回應內容：{result.final_output}")


async def main():
    print("=== 開始基礎測試 ===\n")

    # 不需要 API 的測試
    factory = test_factory_init()
    agent = test_get_agent(factory)
    test_get_agent_not_found(factory)
    test_config_validation_fail()

    # 需要 API 的測試（最後執行）
    print("\n--- API 呼叫測試 ---")
    await test_run_agent(agent)

    print("\n=== 測試完成 ===")


if __name__ == "__main__":
    asyncio.run(main())