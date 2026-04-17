from __future__ import annotations

from agents import Agent, ModelSettings, OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from .agent_utils import import_by_path, make_dynamic_prompt, load_and_format_with_escape
from .config_schema import AgentConfig


class AgentBuilder:
    """負責從 AgentConfig 建立 Agent 實例。"""

    @staticmethod
    def build(agent_config: AgentConfig) -> Agent:
        """從驗證後的 AgentConfig 建立並回傳 Agent 實例。

        Args:
            agent_config: 已通過 Pydantic 驗證的 agent 設定。

        Returns:
            對應的 Agent 實例。
        """
        agent_name = agent_config.name

        model_params = agent_config.model_params
        client = AsyncOpenAI(**model_params.client)
        model_settings = ModelSettings(**model_params.params)
        model_name = model_params.model

        output_schema = (
            import_by_path(agent_config.output_schema)
            if agent_config.output_schema
            else None
        )

        model = OpenAIChatCompletionsModel(model=model_name, openai_client=client)

        instruction_params = agent_config.model_instruction
        dynamic_instruction_flag = instruction_params.dynamic_prompt
        instruction_file_path = instruction_params.instruction_file_path

        if dynamic_instruction_flag:
            dynamic_function_path = instruction_params.dynamic_module_path
            model_context_path = instruction_params.model_context_path
            payload_builder_function = import_by_path(dynamic_function_path)

            context_type, dynamic_instruction = make_dynamic_prompt(
                type_class_path=model_context_path,
                template_path=instruction_file_path,
                payload_builder=payload_builder_function,
            )

            return Agent[context_type](
                name=agent_name,
                instructions=dynamic_instruction,
                model_settings=model_settings,
                model=model,
                output_type=output_schema,
            )
        else:
            instruction = load_and_format_with_escape(instruction_file_path)
            return Agent(
                name=agent_name,
                instructions=instruction,
                model_settings=model_settings,
                model=model,
                output_type=output_schema,
            )
