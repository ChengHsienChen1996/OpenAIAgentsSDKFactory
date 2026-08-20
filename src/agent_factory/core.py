from __future__ import annotations

import os
from typing import Callable, Dict

from dotenv import load_dotenv
from agents import Agent

from .config_loader import AgentConfigLoader
from .agent_builder import AgentBuilder
from .config_schema import AgentConfig
from .rate_limiter.limits_parameters import registry as limit_registry
from .rate_limiter.token_bucket import SOURCE_YAML

load_dotenv()

_RegistryEntry = Callable[[dict], Agent]


class AgentFactory:
    def __init__(self, yaml_settings_path: os.PathLike | str):
        self._registry: Dict[str, Agent] = {}

        for agent_config in AgentConfigLoader.load_validated(yaml_settings_path):
            self._register_model_limits(agent_config)
            self._registry[agent_config.name] = AgentBuilder.build(agent_config)

    @staticmethod
    def _register_model_limits(agent_config: AgentConfig) -> None:
        """把 agent YAML 宣告的 limits 註冊進全域 limit registry。

        優先序 YAML > MODEL_LIMITS > DEFAULT_POLICY。未宣告 ``limits`` 的 agent
        完全不觸碰 registry，既有設定的行為因此保持不變。
        """
        limits = agent_config.model_params.limits
        if limits is None:
            return

        limit_registry.register(
            agent_config.model_params.model,
            limits.to_registry_config(),
            source=SOURCE_YAML,
        )

    def get_agent_by_name(self, name: str) -> Agent:
        """依名稱從 registry 取得 Agent 實例。

        Args:
            name: YAML 設定中 agent 的 ``name`` 欄位值。

        Returns:
            對應的 Agent 實例。

        Raises:
            KeyError: 找不到對應名稱的 agent。
        """
        try:
            return self._registry[name]  # type: ignore
        except KeyError:
            raise KeyError(f"Agent {name} not registered")

    def register(self, name: str):
        """ 使用方式
        factory = create_agent_factory()

        @factory.register("CustomAgent")
        def my_builder(config): ...
        """
        def decorator(func: _RegistryEntry):
            self._registry[name] = func
            return func

        return decorator

    @classmethod
    def create_factory_from_yaml(cls, yaml_settings_path: os.PathLike | str) -> AgentFactory:
        """從指定 YAML 路徑建立 AgentFactory 實例。

        Args:
            yaml_settings_path: YAML 設定檔路徑。

        Returns:
            初始化完成的 AgentFactory。
        """
        return cls(yaml_settings_path)


def create_agent_factory() -> AgentFactory:
    """從環境變數 ``YAML_SETTINGS_FILE`` 建立並回傳 AgentFactory。"""
    return AgentFactory.create_factory_from_yaml(yaml_settings_path=os.getenv("YAML_SETTINGS_FILE"))
