"""Versioned contracts for document layout and evidence-based comparison."""
from __future__ import annotations

from typing import Literal
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from workbench_platform.agent.credentials import normalize_url


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', validate_assignment=True)


class ProviderConfig(Contract):
    id: str = Field(default='', max_length=80)
    version: int = Field(default=1, ge=1)
    name: str = Field(default='自定义供应商', min_length=1, max_length=120)
    protocol: Literal['openai_chat_completions', 'anthropic_messages'] = 'openai_chat_completions'
    base_url: str
    model: str = Field(min_length=1, max_length=200)
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    context_window: int = Field(default=32768, ge=2048, le=2_000_000)
    max_output_tokens: int = Field(default=16384, ge=256, le=200_000)
    supports_vision: bool = False
    structured_output: Literal['json_object', 'json_schema', 'prompt'] = 'prompt'
    total_seconds: int = Field(default=180, ge=10, le=1800)
    max_request_bytes: int = Field(default=12 * 1024 * 1024, ge=65536, le=32 * 1024 * 1024)
    image_max_bytes: int = Field(default=8 * 1024 * 1024, ge=65536, le=8 * 1024 * 1024)
    max_image_edge: int = Field(default=2560, ge=512, le=8192)
    vision_tokens_per_tile: int = Field(default=512, ge=64, le=8192)

    @field_validator('base_url')
    @classmethod
    def valid_url(cls, value):
        return normalize_url(value)

    @model_validator(mode='after')
    def budget(self):
        if self.max_output_tokens >= self.context_window:
            raise ValueError('输出上限必须小于模型上下文容量')
        return self


class LayoutParseConfig(Contract):
    mode: Literal['local_ocr', 'multimodal'] = 'local_ocr'
    engine: Literal['paddlevl', 'mineru_pro'] = 'paddlevl'
    provider_id: str | None = Field(default=None, max_length=80)
    provider_version: int | None = Field(default=None, ge=1)
    max_image_edge: int = Field(default=2560, ge=512, le=8192)
    image_max_bytes: int = Field(default=8 * 1024 * 1024, ge=65536, le=8 * 1024 * 1024)
    max_output_tokens: int = Field(default=8192, ge=256, le=200_000)
    vision_tokens_per_tile: int = Field(default=512, ge=64, le=8192)
    fallback_local: bool = False

    @model_validator(mode='after')
    def connection(self):
        if self.mode == 'multimodal' and not self.provider_id:
            raise ValueError('多模态解析需要选择供应商连接')
        return self


class ComparisonField(Contract):
    key: str = Field(pattern=r'^[a-z][a-z0-9_]{0,63}$')
    label: str = Field(min_length=1, max_length=100)
    type: Literal['text', 'date', 'number', 'percentage'] = 'text'
    aliases: list[str] = Field(default_factory=list, max_length=50)
    tolerance: float = Field(default=0, ge=0, allow_inf_nan=False)


def progress_fields():
    return [ComparisonField(key=key, label=label, type=kind) for key, label, kind in [
        ('project', '项目', 'text'), ('work_item', '工作项', 'text'),
        ('milestone', '里程碑', 'text'), ('owner', '负责人', 'text'),
        ('planned_date', '计划日期', 'date'), ('actual_date', '实际日期', 'date'),
        ('completion', '完成率', 'percentage'), ('status', '状态', 'text'),
        ('risk', '风险', 'text'), ('dependency', '依赖', 'text')]]


class ComparisonProfile(Contract):
    id: str = Field(default='project_progress', pattern=r'^[a-zA-Z0-9_-]{1,80}$')
    version: int = Field(default=1, ge=1)
    name: str = Field(default='项目进度', min_length=1, max_length=120)
    instructions: str = Field(default='比较两侧材料中与项目进度相关的事实，按项目和工作项对齐并保留原文证据。', min_length=1, max_length=12000)
    fields: list[ComparisonField] = Field(default_factory=progress_fields, min_length=1, max_length=50)
    entity_aliases: dict[str, list[str]] = Field(default_factory=dict)
    date_policy: Literal['same_as_of', 'ignore_as_of'] = 'same_as_of'
    as_of_a: str | None = Field(default=None, max_length=100)
    as_of_b: str | None = Field(default=None, max_length=100)

    @model_validator(mode='after')
    def unique_keys(self):
        if len({field.key for field in self.fields}) != len(self.fields):
            raise ValueError('比较字段 key 不能重复')
        field_names = {}
        for field in self.fields:
            for name in [field.key, field.label, *field.aliases]:
                normalized = ''.join(unicodedata.normalize('NFKC', name).casefold().split())
                if not normalized or (normalized in field_names and field_names[normalized] != field.key):
                    raise ValueError('字段名称或别名不能重复指向不同字段')
                field_names[normalized] = field.key
        aliases = {}
        for canonical, values in self.entity_aliases.items():
            if not canonical.strip() or len(values) > 100:
                raise ValueError('实体别名配置无效')
            for value in [canonical, *values]:
                normalized = ''.join(unicodedata.normalize('NFKC', value).casefold().split())
                if not normalized or (normalized in aliases and aliases[normalized] != canonical):
                    raise ValueError('同一实体别名不能指向不同实体')
                aliases[normalized] = canonical
        return self


class EvidenceInput(Contract):
    block_id: str = Field(min_length=1, max_length=200)
    quote: str = Field(min_length=1, max_length=16000)


class FactInput(Contract):
    field: str = Field(min_length=1, max_length=100)
    entity: str = Field(min_length=1, max_length=500)
    work_item: str = Field(default='', max_length=500)
    value: str = Field(min_length=1, max_length=16000)
    as_of: str | None = Field(default=None, max_length=100)
    evidence: list[EvidenceInput] = Field(min_length=1, max_length=20)


class FactExtraction(Contract):
    facts: list[FactInput] = Field(max_length=500)


class SemanticDecision(Contract):
    pair_id: str
    status: Literal['consistent', 'different', 'insufficient']
    explanation: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(min_length=1, max_length=40)


class SemanticDecisions(Contract):
    decisions: list[SemanticDecision] = Field(max_length=100)


class FactSet(Contract):
    kind: Literal['clear.fact_set'] = 'clear.fact_set'
    schema_version: Literal['1.0'] = '1.0'
    side: Literal['a', 'b']
    document_id: str
    revision: int
    facts: list[dict] = Field(default_factory=list)
    diagnostics: list[dict] = Field(default_factory=list)
    coverage: dict = Field(default_factory=dict)


class ComparisonResult(Contract):
    kind: Literal['clear.comparison'] = 'clear.comparison'
    schema_version: Literal['1.0'] = '1.0'
    template_version: Literal['clear-comparison-html-v1'] = 'clear-comparison-html-v1'
    profile: ComparisonProfile
    documents: dict
    fact_sets: dict
    items: list[dict]
    summary: dict
    coverage: dict
    diagnostics: list[dict] = Field(default_factory=list)
    models: list[dict] = Field(default_factory=list)
    source_updated: bool = False
