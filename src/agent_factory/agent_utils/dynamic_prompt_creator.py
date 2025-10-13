from pathlib import Path
from typing import Any, Dict, Callable, Tuple

from .module_loader import import_by_path

PayloadBuilder = Callable[[Any, Any], Dict[str, Any]]  # (context, agent) -> payload dict


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
