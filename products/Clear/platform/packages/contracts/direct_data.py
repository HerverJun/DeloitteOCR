"""Explicit sequential cleaning and comparison requests over fixed operators."""
from typing import Literal
from pydantic import Field, model_validator
from .agent import AgentModel, Identity
from . import data_operations as data


StepOperator = data.DataStep.model_fields['step'].annotation


class ActionStep(AgentModel):
    side: Literal['left', 'right'] = 'left'
    step: StepOperator = Field(discriminator='operator')


class DataActionRequest(AgentModel):
    request_id: Identity
    action: Literal['clean', 'compare']
    name: str = Field(default='明确数据处理', min_length=1, max_length=100)
    left_asset_id: Identity
    right_asset_id: Identity | None = None
    left_options: data.InputOptions = Field(default_factory=data.InputOptions)
    right_options: data.InputOptions = Field(default_factory=data.InputOptions)
    steps: list[ActionStep] = Field(default_factory=list, max_length=32)
    compare_spec: data.CompareSpec | None = None
    expected_scope_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode='after')
    def fixed_action(self):
        if self.action == 'compare':
            if not self.right_asset_id or self.compare_spec is None:
                raise ValueError('双表核对需要明确右表与列配对条件')
            if self.left_asset_id == self.right_asset_id:
                raise ValueError('双表核对需要两份不同的固定输入')
        elif self.right_asset_id is not None or self.compare_spec is not None or any(item.side != 'left' for item in self.steps):
            raise ValueError('单表清洗只能操作左表，不能包含双表核对参数')
        if self.action == 'clean' and not self.steps:
            raise ValueError('请明确至少一个已有清洗算子及列参数')
        return self
