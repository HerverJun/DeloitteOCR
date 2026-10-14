"""Load an explicitly configured, verified F-drive Agent import layer."""
import json
import os
from pathlib import Path
import sys

from workbench_platform.supervisor.processes import require_f_path


def load_dependencies():
    configured = os.environ.get('WORKBENCH_AGENT_LAYER')
    if configured:
        root = require_f_path(Path(configured), 'Agent dependency layer')
        receipt = json.loads((root / 'layer-receipt.json').read_text(encoding='utf-8'))
        if receipt.get('format') != 'verified-installed-layer-v1':
            raise ValueError('Unknown Agent layer receipt')
        if str(root) not in sys.path:
            sys.path.append(str(root))
    # Disabled Agent never calls this function; native tools stay independent.
    from langgraph.graph import StateGraph
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    return StateGraph, AsyncSqliteSaver
