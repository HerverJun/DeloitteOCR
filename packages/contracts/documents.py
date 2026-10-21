"""Small public commands for durable document parsing and literal table review."""
from typing import Literal
from pydantic import Field
from .agent import AgentModel, Identity, Hash


class DocumentParse(AgentModel):
    schema_version: Literal['1.0'] = '1.0'
    request_id: Identity
    asset_id: Identity
    asset_revision: int = Field(ge=1)
    blob_hash: Hash
    ocr_policy: Literal['auto', 'never'] = 'auto'
    ocr_link_id: Identity | None = None


class TableCell(AgentModel):
    row: int = Field(ge=0, le=1000000)
    column: int = Field(ge=0, le=16383)
    row_span: int = Field(default=1, ge=1, le=1000)
    column_span: int = Field(default=1, ge=1, le=1000)
    text: str = Field(max_length=32767)
    confidence: float | None = None
    polygon: list | None = None
    is_header: bool = False


class TableReview(AgentModel):
    expected_revision: int = Field(ge=1)
    cells: list[TableCell] = Field(min_length=1, max_length=100000)
