"""Typed deterministic requests; accepted driver requests remain immutable."""
from typing import Literal
from pydantic import Field, model_validator
from .agent import AgentModel, Identity


class ImageTextRequest(AgentModel):
    request_id: Identity
    engines: list[Identity] = Field(default_factory=list, max_length=16)
    link_id: Identity | None = None
    expected_scope_revision: int | None = Field(default=None, ge=1)


class PdfTextRequest(AgentModel):
    request_id: Identity
    page_numbers: list[int] = Field(min_length=1, max_length=100)
    mode: Literal['auto', 'native', 'ocr'] = 'auto'
    engine: Literal['auto', 'ppocr', 'paddlevl', 'glm', 'hunyuan', 'mineru_pro'] = 'auto'
    link_id: Identity | None = None
    expected_scope_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode='after')
    def explicit_pages(self):
        if len(set(self.page_numbers)) != len(self.page_numbers) or any(page < 1 or page > 1000 for page in self.page_numbers):
            raise ValueError('请填写不重复的 1—1000 页码；单次最多选择 100 页')
        self.page_numbers = sorted(self.page_numbers)
        return self
