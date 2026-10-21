"""Optional general-Agent connection contracts, without runtime dependencies."""
from typing import Literal
from urllib.parse import urlsplit
from pydantic import Field, model_validator
from .agent import AgentModel


class McpServer(AgentModel):
    name: str = Field(pattern=r'^[A-Za-z0-9_-]{1,32}$')
    transport: Literal['stdio', 'streamable-http']
    enabled: bool = True
    command: str | None = Field(default=None, max_length=2048)
    args: list[str] = Field(default_factory=list, max_length=100)
    url: str | None = Field(default=None, max_length=2048)
    env: dict[str, str] = Field(default_factory=dict, max_length=100)
    headers: dict[str, str] = Field(default_factory=dict, max_length=50)

    @model_validator(mode='after')
    def transport_fields(self):
        if self.transport == 'stdio':
            if not self.command or self.url or self.headers:
                raise ValueError('stdio连接需要command，不接受url或headers')
        else:
            parsed = urlsplit(self.url or '')
            if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise ValueError('MCP URL无效，请使用headers提供凭据')
            if self.command or self.args or self.env:
                raise ValueError('HTTP连接不接受本地进程参数')
        return self


class Integrations(AgentModel):
    revision: int = Field(default=0, ge=0)
    subagents: bool = True
    mcp_servers: list[McpServer] = Field(default_factory=list, max_length=20)

    @model_validator(mode='after')
    def distinct(self):
        if len({server.name for server in self.mcp_servers}) != len(self.mcp_servers):
            raise ValueError('MCP server names must be unique')
        return self
