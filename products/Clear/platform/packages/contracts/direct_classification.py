"""Explicit existing-model prediction requests."""
from typing import Literal
from pydantic import Field, model_validator
from .agent import AgentModel, Identity


class PredictRequest(AgentModel):
    request_id: Identity
    link_id: Identity | None = None
    model_id: Identity | None = None
    use_existing_model: bool = False
    model_name: str | None = Field(default=None, min_length=1, max_length=100)
    device: Literal['cpu', 'cuda'] = 'cpu'
    expected_scope_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode='after')
    def existing_model_only(self):
        if not self.model_id and not self.use_existing_model:
            raise ValueError('请指定已有模型，或明确使用当前已有模型')
        if self.model_name and (not self.use_existing_model or self.model_id):
            raise ValueError('模型名称仅用于选择已有模型，不能同时指定模型 ID')
        return self
