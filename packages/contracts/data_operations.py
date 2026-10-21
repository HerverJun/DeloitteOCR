"""Strict existing Data operator contracts shared by every execution path."""
from __future__ import annotations
from typing import Annotated, Literal, Union
from pydantic import Field, model_validator
from .agent import AgentModel, Identity, Hash


class DataCreate(AgentModel):
    name: str = Field(min_length=1, max_length=100)


class InputOptions(AgentModel):
    sheet_name: str | None = Field(default=None, min_length=1, max_length=256)
    header_row: int = Field(default=1, ge=1, le=100)
    encoding: Literal['utf-8', 'utf-8-sig', 'gb18030', 'utf-16'] = 'utf-8-sig'
    delimiter: Literal[',', '\t', ';', '|'] = ','


class DataInspect(AgentModel):
    asset_id: Identity
    asset_revision: int = Field(ge=1)
    blob_hash: Hash
    options: InputOptions = Field(default_factory=InputOptions)


class DataWrite(AgentModel):
    data_session_id: Identity
    expected_version: int = Field(ge=1)


class DataImport(DataWrite, DataInspect):
    side: Literal['left', 'right']


class Trim(AgentModel):
    operator: Literal['trim']
    column: str = Field(pattern=r'^c[1-9][0-9]{0,2}$')


class TrimColumns(AgentModel):
    operator: Literal['trim_columns']
    columns: list[str] = Field(min_length=1, max_length=256)

    @model_validator(mode='after')
    def valid_columns(self):
        import re
        if len(set(self.columns)) != len(self.columns) or any(not re.fullmatch(r'c[1-9][0-9]{0,2}', value) for value in self.columns):
            raise ValueError('Columns must be distinct existing column IDs')
        return self


class Rename(Trim):
    operator: Literal['rename']
    header: str = Field(min_length=1, max_length=256)


class MapValues(Trim):
    operator: Literal['map']
    values: dict[str, str] = Field(min_length=1, max_length=1000)


class Cast(Trim):
    operator: Literal['cast']
    target: Literal['decimal', 'integer', 'boolean', 'date']


class Filter(Trim):
    operator: Literal['filter']
    predicate: Literal['equals', 'not_equals', 'is_null', 'not_null']
    value: str | bool | None = None

    @model_validator(mode='after')
    def valid_filter(self):
        if self.predicate in {'equals', 'not_equals'} and self.value is None:
            raise ValueError('Filter value is required')
        if self.predicate in {'is_null', 'not_null'} and self.value is not None:
            raise ValueError('Null filter does not accept a value')
        return self


class Duplicates(AgentModel):
    operator: Literal['duplicates']
    columns: list[str] = Field(min_length=1, max_length=256)
    include_null: bool = False


class TypedCompare(Trim):
    operator: Literal['filter_compare']
    predicate: Literal['gt', 'ge', 'lt', 'le', 'between']
    value_kind: Literal['decimal', 'date', 'text']
    value: str = Field(max_length=4096)
    upper: str | None = Field(default=None, max_length=4096)

    @model_validator(mode='after')
    def interval(self):
        if (self.predicate == 'between') != (self.upper is not None):
            raise ValueError('Only between requires an upper bound')
        return self


class GroupSummary(AgentModel):
    operator: Literal['group_summary']
    columns: list[str] = Field(min_length=1, max_length=256)
    amount_columns: list[str] = Field(default_factory=list, max_length=256)


class DataStep(DataWrite):
    side: Literal['left', 'right']
    step: Annotated[Union[Trim, TrimColumns, Rename, MapValues, Cast, Filter, Duplicates, TypedCompare, GroupSummary], Field(discriminator='operator')]


class CompareSpec(AgentModel):
    key_pairs: list[list[str]] = Field(min_length=1, max_length=256)
    compare_pairs: list[list[str]] = Field(default_factory=list, max_length=256)
    amount_pairs: list[list[str]] = Field(default_factory=list, max_length=256, description='金额列对必须同时出现在 compare_pairs 中')
    type_policy: Literal['strict', 'text'] = 'strict'
    null_policy: Literal['never_match', 'equal'] = 'never_match'
    amount_tolerance: str = Field(default='0', pattern=r'^\d+(\.\d+)?$')
    trim_text: bool = False
    casefold_text: bool = False

    @model_validator(mode='after')
    def pairs(self):
        import re
        for pair in [*self.key_pairs, *self.compare_pairs, *self.amount_pairs]:
            if len(pair) != 2 or any(not re.fullmatch(r'c[1-9][0-9]{0,2}', column) for column in pair):
                raise ValueError('Column pairs must contain two queried column IDs')
        if any(pair not in self.compare_pairs for pair in self.amount_pairs):
            raise ValueError('Amount pairs must also be comparison pairs')
        return self


class DataCompare(DataWrite):
    spec: CompareSpec


class DataPage(AgentModel):
    data_session_id: Identity
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=50)


class DataTable(DataPage):
    side: Literal['left', 'right']
    revision: Identity | None = None


class DataResult(DataPage):
    result_id: Identity
    category: Literal['same', 'different', 'only_left', 'only_right', 'ambiguous'] | None = None


class DataExport(DataWrite):
    result_id: Identity | None = None
    side: Literal['left', 'right'] | None = None
    categories: list[Literal['same', 'different', 'only_left', 'only_right', 'ambiguous']] | None = Field(default=None, min_length=1, max_length=5)

    @model_validator(mode='after')
    def target(self):
        if bool(self.result_id) == bool(self.side):
            raise ValueError('Choose comparison result or one table')
        if self.side and self.categories:
            raise ValueError('Categories apply only to comparison results')
        return self
