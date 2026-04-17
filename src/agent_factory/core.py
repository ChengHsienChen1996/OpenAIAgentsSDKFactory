import os

from pathlib import Path
from dotenv import load_dotenv
from typing import Union, Dict, Callable
from omegaconf import OmegaConf, DictConfig, ListConfig

from openai import AsyncOpenAI
from agents import Agent, ModelSettings, OpenAIChatCompletionsModel

from .agent_utils import *
from .config_schema import AgentConfig

load_dotenv()
AgentBuilder = Callable[[dict], "Agent"]  # 參數是 settings dict


class AgentFactory:
    def __init__(self, yaml_settings_path: os.PathLike | str):
        self._registry: Dict[str, AgentBuilder] = {}
        self._settings_file = Path(yaml_settings_path)
        self._file_dir = self._settings_file.parent


        self._register_agent_from_setting()


    def load_agent_setup(self) -> Union[ListConfig, DictConfig]:
        yaml_settings = load_yaml(self._settings_file)
        return OmegaConf.merge({"__dir__": str(self._file_dir)}, yaml_settings)


    def _register_agent_from_setting(self):
        settings = self.load_agent_setup()

        for agent_type, agents in settings.agents.items():
            for agent in agents:
                agent_name, agent_settings = tuple(agent.items())[0]
                try:
                    AgentConfig.model_validate(
                        OmegaConf.to_container(agent_settings, resolve=True)
                    )
                except Exception as exc:
                    raise ValueError(
                        f"Agent '{agent_name}' 設定驗證失敗：{exc}"
                    ) from exc
                self._registry[agent_name] = self.create_agent(agent_settings) #type: ignore


    @staticmethod
    def create_agent(agent_config: DictConfig) -> Agent:
        agent_name = agent_config.name

        model_params = agent_config.model_params
        client = AsyncOpenAI(**model_params.client)
        model_settings = ModelSettings(**model_params.params)

        model_name = model_params.model

        output_schema_params = agent_config.get("output_schema")
        output_schema = import_by_path(output_schema_params) if output_schema_params else None

        model = OpenAIChatCompletionsModel(model=model_name,
                                           openai_client=client)

        instruction_params = agent_config.model_instruction
        dynamic_instruction_flag = instruction_params.dynamic_prompt
        instruction_file_path = instruction_params.instruction_file_path

        if dynamic_instruction_flag:
            dynamic_function_path = instruction_params.dynamic_module_path
            model_context_path = instruction_params.model_context_path
            payload_builder_function = import_by_path(dynamic_function_path)

            context_type, dynamic_instruction = make_dynamic_prompt(type_class_path=model_context_path,
                                                                    template_path=instruction_file_path,
                                                                    payload_builder=payload_builder_function)

            return Agent[context_type](name=agent_name,
                                       instructions=dynamic_instruction,
                                       model_settings=model_settings,
                                       model=model,
                                       output_type=output_schema)
        else:
            instruction = load_and_format_with_escape(instruction_file_path)
            return Agent(name=agent_name,
                         instructions=instruction,
                         model_settings=model_settings,
                         model=model,
                         output_type=output_schema)


    @classmethod
    def register(cls, name: str):
        def decorator(func: AgentBuilder):
            cls._registry[name] = func
            return func
        return decorator


    @classmethod
    def create_factory_from_yaml(cls, yaml_settings_path: os.PathLike | str):
        return cls(yaml_settings_path)

    def get_agent_by_name(self, name: str) -> Agent:
        try:
            return self._registry[name] #type: ignore
        except KeyError:
            raise KeyError(f"Agent {name} not registered")


def create_agent_factory() -> AgentFactory:
    return AgentFactory.create_factory_from_yaml(yaml_settings_path=os.getenv("YAML_SETTINGS_FILE"))


def test_factory():
    a = AgentFactory
    print(123)