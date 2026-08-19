"""AgentConfigLoader 的單元測試（YAML 載入與 Pydantic 驗證）。

由原 tests/test_basic.py 的 test_config_validation_fail 拆分並擴充而來。
"""
import textwrap

import pytest

from agent_factory.config_loader import AgentConfigLoader
from agent_factory.config_schema import AgentConfig
from agent_factory.core import AgentFactory


def test_load_raw_injects_dir(sample_yaml_path):
    """load_raw 會注入 __dir__，其值為 YAML 檔所在目錄。

    注意：必須以 key 取值，`raw.__dir__` 取到的是 Python 內建的 __dir__ 方法。
    """
    raw = AgentConfigLoader.load_raw(sample_yaml_path)

    assert raw["__dir__"] == str(sample_yaml_path.parent)


def test_load_validated_returns_agent_configs(sample_yaml_path):
    """load_validated 回傳通過驗證的 AgentConfig 列表。"""
    configs = AgentConfigLoader.load_validated(sample_yaml_path)

    assert len(configs) == 1
    assert isinstance(configs[0], AgentConfig)
    assert configs[0].name == "SampleAgent"


def test_load_validated_resolves_dir_in_instruction_path(sample_yaml_path):
    """instruction_file_path 中的 ${__dir__} 應已解析為實際路徑且檔案存在。"""
    config = AgentConfigLoader.load_validated(sample_yaml_path)[0]
    instruction_path = config.model_instruction.instruction_file_path

    assert "${__dir__}" not in instruction_path
    assert (sample_yaml_path.parent / "prompt_files" / "sample_instruction.md").exists()


def _write_yaml(tmp_path, content: str):
    yaml_file = tmp_path / "invalid_agents.yaml"
    yaml_file.write_text(textwrap.dedent(content), encoding="utf-8")
    return yaml_file


def test_load_validated_missing_dynamic_fields_raises_value_error(tmp_path):
    """dynamic_prompt=true 卻缺少必填欄位時拋出 ValueError，訊息含 agent 的 YAML key。"""
    yaml_file = _write_yaml(
        tmp_path,
        """
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
                    base_url: https://example.invalid
        """,
    )

    with pytest.raises(ValueError, match="bad_agent"):
        AgentConfigLoader.load_validated(yaml_file)


def test_load_validated_missing_base_url_raises_value_error(tmp_path):
    """client 缺少必填的 base_url 時拋出 ValueError。"""
    yaml_file = _write_yaml(
        tmp_path,
        """
        agents:
          test:
            - no_base_url_agent:
                name: NoBaseUrlAgent
                model_instruction:
                  dynamic_prompt: false
                  instruction_file_path: some/path.md
                model_params:
                  model: gpt-4.1
                  client:
                    api_key: test
        """,
    )

    with pytest.raises(ValueError, match="base_url"):
        AgentConfigLoader.load_validated(yaml_file)


def test_factory_propagates_validation_error(tmp_path):
    """設定錯誤時，AgentFactory 建立階段即拋出 ValueError（原 test_basic 的覆蓋範圍）。"""
    yaml_file = _write_yaml(
        tmp_path,
        """
        agents:
          test:
            - bad_agent:
                name: BadAgent
                model_instruction:
                  dynamic_prompt: true
                  instruction_file_path: some/path.md
                model_params:
                  model: gemma-4-31b-it
                  client:
                    api_key: test
                    base_url: https://example.invalid
        """,
    )

    with pytest.raises(ValueError):
        AgentFactory.create_factory_from_yaml(yaml_file)
