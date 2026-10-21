"""Load an explicitly configured, verified F-drive Agent import layer."""
import json
import os
from pathlib import Path
import sys

from workbench_platform.supervisor.processes import require_f_path


def load_support_dependencies():
    configured = os.environ.get('WORKBENCH_AGENT_LAYER')
    if configured:
        root = require_f_path(Path(configured), 'Agent dependency layer')
        receipt = json.loads((root / 'layer-receipt.json').read_text(encoding='utf-8'))
        if receipt.get('format') != 'verified-installed-layer-v1':
            raise ValueError('Unknown Agent layer receipt')
        if str(root) not in sys.path:
            sys.path.append(str(root))
    # YAML support is independent of the retired execution engine.


def load_dependencies():
    """Historical checkpoint reader only; production execution uses Harness."""
    load_support_dependencies()
    from langgraph.graph import StateGraph
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    return StateGraph, AsyncSqliteSaver
