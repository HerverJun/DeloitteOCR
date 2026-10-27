"""Use the portable install policy without weakening standalone F-only storage."""
import os
from pathlib import Path


def require_path(value, label='Classification path', *, role='write'):
    try:
        from packages.runtime import require_path as validate
    except ModuleNotFoundError as error:
        if error.name not in {'packages', 'packages.runtime'}:
            raise
        if os.environ.get('CLEAR_INSTALL_CONFIG'):
            raise RuntimeError('Managed classification runtime is missing packages.runtime') from error
        requested = Path(value).absolute()
        resolved = requested.resolve()
        if requested.drive.upper() != 'F:' or resolved.drive.upper() != 'F:':
            raise ValueError(f'{label} must remain on the local F drive: {requested}')
        return resolved
    return validate(value, label, role=role)
