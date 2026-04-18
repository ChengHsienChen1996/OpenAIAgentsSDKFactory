from __future__ import annotations

import warnings
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

    @model_validator(mode="after")
    def validate_client_fields(self) -> ModelParamsConfig:
        """檢查 client dict 的必要欄位。

        - ``base_url``：必填，缺少時拋出 ValueError。
        - ``api_key``：選填，缺少時發出 warnings.warn（部分供應商允許從環境變數注入）。
        """
        if "base_url" not in self.client:
            raise ValueError(
                "ModelParamsConfig.client 缺少必填欄位：base_url"
            )
        if "api_key" not in self.client:
            warnings.warn(
                "ModelParamsConfig.client 未設定 api_key，"
                "將依賴環境變數注入（如 OPENAI_API_KEY）。",
                UserWarning,
                stacklevel=2,
            )
        return self


class AgentConfig(BaseModel):
    name: str
    model_instruction: InstructionConfig
    model_params: ModelParamsConfig
    output_schema: Optional[str] = None
