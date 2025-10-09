import os
import importlib

from pathlib import Path
from typing import Any, Optional, Union, Dict, Callable, Tuple

from omegaconf import OmegaConf, DictConfig, ListConfig


PayloadBuilder = Callable[[Any, Any], Dict[str, Any]]  # (context, agent) -> payload dict


def import_by_path(dotted: str) -> Any:
    mod_path, _, attr = dotted.rpartition(".")
    if not mod_path:
        raise ValueError(f"Invalid dotted path: {dotted}")
    mod = importlib.import_module(mod_path)
    return getattr(mod, attr)

def load_and_format_with_escape(path: os.PathLike| str, kwargs = None) -> Optional[str]:
    with open(path, encoding="utf-8") as f:
        template = f.read()

    if kwargs:
        try:
            kwargs = {k: v for k, v in kwargs.items() if v not in (None, "")}
            return template.format(**kwargs)
        except KeyError as e:
            print(f"KeyError: {e}")
    else:
        return template


def load_yaml(yaml_path: os.PathLike | str) -> Union[DictConfig, ListConfig]:
    with open(yaml_path, "r") as f:
        yaml_file = OmegaConf.load(f)

    return yaml_file


def make_dynamic_prompt(
    type_class_path: str,
    template_path: str,
    payload_builder: PayloadBuilder,
    extra_vars: Dict[str, Any] | None = None,
) -> Tuple[Any, Callable[[Any, Any], str]]:
    _Type = import_by_path(type_class_path)
    _template = Path(template_path).read_text(encoding="utf-8")
    _extra = dict(extra_vars or {})

    def dynamic_instructions(context: "RunContextWrapper[_Type]", agent: "Agent[_Type]") -> str:  # noqa: F821
        payload = payload_builder(context, agent)
        payload.update(_extra)
        return _template.format(**payload)

    return _Type, dynamic_instructions
