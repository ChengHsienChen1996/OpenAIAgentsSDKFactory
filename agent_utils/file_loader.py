import os

from typing import Optional, Union
from omegaconf import OmegaConf, DictConfig, ListConfig


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