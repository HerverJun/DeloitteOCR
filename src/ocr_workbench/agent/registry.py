"""Finite tool registry: whole-batch validation before dispatch and recheck on use."""
from .contracts import TOOL_INPUTS, ToolResult, validate_call_batch
from .policy import PERMISSIONS, PolicyDenied


class ToolRegistry:
    def __init__(self, policy):
        self.policy = policy
        self.handlers = {}

    def register(self, name, handler):
        if name not in TOOL_INPUTS or name in self.handlers:
            raise ValueError("Unknown or duplicate tool")
        self.handlers[name] = handler

    def validate_batch(self, run, generation, calls, *, complete=True):
        parsed = validate_call_batch(calls, complete=complete)
        for call in parsed:
            if call.tool not in self.handlers:
                raise PolicyDenied("unsupported_capability", "该工具尚未在当前工作台启用")
            self.policy.authorize(run, generation, call.tool, call.arguments)
        return parsed

    async def execute(self, run, generation, call, context):
        # GraphInterrupt must propagate. Never wrap an interrupt in a generic
        # tool-failure handler (B00 proves it inherits Exception).
        arguments = self.policy.authorize(run, generation, call.tool, call.arguments)
        if call.tool not in self.handlers:
            raise PolicyDenied("unsupported_capability", "该工具未启用")
        result = await self.handlers[call.tool](arguments, context)
        return ToolResult.model_validate(result).model_dump(mode="json")

    def catalog(self):
        return {name: {"input_schema": model.model_json_schema(), "permission": PERMISSIONS[name], "available": name in self.handlers}
                for name, model in TOOL_INPUTS.items()}
