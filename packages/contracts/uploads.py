"""Durable per-file uploads with fixed request identity and receipt."""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from packages.contracts.models import Asset, Error, Hash, Id, Timestamped, Utc, Wire

UPLOAD_MAX_BYTES = 128 * 1024 * 1024
UploadName = Annotated[str, Field(min_length=1, max_length=240)]
UploadSize = Annotated[int, Field(strict=True, ge=1, le=UPLOAD_MAX_BYTES)]


class UploadBinding(Wire):
    workspace_id: Id
    request_id: Id
    name: UploadName
    sha256: Hash
    size_bytes: UploadSize

    @field_validator('name')
    @classmethod
    def safe_filename(cls, value: str) -> str:
        reserved = {'CON', 'PRN', 'AUX', 'NUL',
                    *(f'COM{n}' for n in range(1, 10)),
                    *(f'LPT{n}' for n in range(1, 10)),
                    *(f'COM{n}' for n in ('¹', '²', '³')),
                    *(f'LPT{n}' for n in ('¹', '²', '³'))}
        stem = value.split('.', 1)[0].rstrip(' .').upper()
        if (value != value.strip() or value in {'.', '..'} or value.endswith('.')
                or stem in reserved
                or any(char in value for char in '/\\:<>"|?*')
                or any(ord(char) < 32 for char in value)):
            raise ValueError('Upload name must be a safe single Windows filename')
        return value


def upload_payload_hash(binding: UploadBinding) -> str:
    canonical = json.dumps(binding.model_dump(mode='json'), ensure_ascii=False,
                           sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


class UploadReceipt(Timestamped):
    binding: UploadBinding
    payload_hash: Hash
    asset: Asset
    created_at: Utc

    @model_validator(mode='after')
    def bound_asset(self):
        if self.payload_hash != upload_payload_hash(self.binding):
            raise ValueError('Upload receipt payload hash does not match binding')
        if (self.asset.workspace_id != self.binding.workspace_id
                or self.asset.original_name != self.binding.name
                or self.asset.blob_hash != self.binding.sha256
                or self.asset.size_bytes != self.binding.size_bytes
                or self.asset.storage_mode != 'managed'):
            raise ValueError('Upload receipt asset does not match original binding')
        return self


class UploadStatus(Timestamped):
    binding: UploadBinding
    payload_hash: Hash
    status: Literal['pending', 'committed', 'rejected']
    receipt: UploadReceipt | None = None
    error: Error | None = None
    created_at: Utc
    updated_at: Utc

    @model_validator(mode='after')
    def valid_status(self):
        if self.payload_hash != upload_payload_hash(self.binding):
            raise ValueError('Upload status payload hash does not match binding')
        if self.status == 'committed':
            if (self.receipt is None or self.error is not None
                    or self.receipt.binding != self.binding
                    or self.receipt.payload_hash != self.payload_hash):
                raise ValueError('Committed upload requires its exact fixed receipt')
        elif self.status == 'pending':
            if self.receipt is not None or self.error is not None:
                raise ValueError('Pending upload has no proven final effect')
        else:
            if (self.receipt is not None or self.error is None
                    or self.error.request_id != self.binding.request_id
                    or self.error.details.get('effect') != 'no_registration'):
                raise ValueError('Rejected upload requires proof of no registration')
        return self


UPLOAD_MODELS = [UploadBinding, UploadReceipt, UploadStatus]
