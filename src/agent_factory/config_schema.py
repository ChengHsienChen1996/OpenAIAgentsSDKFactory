from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, model_validator


class InstructionConfig(BaseModel):
    dynamic_prompt: bool
    instruction_file_path: str
    dynamic_module_path: Optional[str] = None
    model_context_path: Optional[str] = None

    @model_validator(mode="after")
    def require_dynamic_fields(self) -> InstructionConfig:
        """當 dynamic_prompt=True 時，dynamic_module_path 與 model_context_path 為必填。"""
        if self.dynamic_prompt:
            missing = [
                field
                for field in ("dynamic_module_path", "model_context_path")
                if getattr(self, field) is None
            ]
            if missing:
                raise ValueError(
                    f"dynamic_prompt=True 時以下欄位為必填：{', '.join(missing)}"
                )
        return self


class ModelParamsConfig(BaseModel):
    model: str
    client: Dict[str, Any] = {}
    params: Dict[str, Any] = {}


class AgentConfig(BaseModel):
    name: str
    model_instruction: InstructionConfig
    model_params: ModelParamsConfig
    output_schema: Optional[str] = None
