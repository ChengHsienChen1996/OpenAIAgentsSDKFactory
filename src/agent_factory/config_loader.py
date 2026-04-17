from __future__ import annotations

import os
from pathlib import Path
from typing import List

from omegaconf import OmegaConf, DictConfig

from .agent_utils.file_loader import load_yaml
from .config_schema import AgentConfig


class AgentConfigLoader:
    """負責從 YAML 載入並驗證 agent 設定。"""

    @staticmethod
    def load_raw(yaml_path: os.PathLike | str) -> DictConfig:
        """載入 YAML 並注入 __dir__，回傳 DictConfig。

        Args:
            yaml_path: YAML 設定檔路徑。

        Returns:
            注入 ``__dir__`` 後的 DictConfig。
        """
        settings_file = Path(yaml_path)
        file_dir = settings_file.parent
        yaml_settings = load_yaml(settings_file)
        return OmegaConf.merge({"__dir__": str(file_dir)}, yaml_settings)

    @staticmethod
    def load_validated(yaml_path: os.PathLike | str) -> List[AgentConfig]:
        """載入 YAML 並對每個 agent 執行 Pydantic 驗證。

        Args:
            yaml_path: YAML 設定檔路徑。

        Returns:
            驗證後的 AgentConfig 列表。

        Raises:
            ValueError: 任何 agent 設定不符合 schema 時，包含 agent YAML key 與欄位路徑。
        """
        settings = AgentConfigLoader.load_raw(yaml_path)
        validated_configs: List[AgentConfig] = []

        for _agent_type, agents in settings.agents.items():
            for agent in agents:
                agent_name, agent_settings = tuple(agent.items())[0]
                try:
                    validated = AgentConfig.model_validate(
                        OmegaConf.to_container(agent_settings, resolve=True)
                    )
                except Exception as exc:
                    raise ValueError(
                        f"Agent '{agent_name}' 設定驗證失敗：{exc}"
                    ) from exc
                validated_configs.append(validated)

        return validated_configs
