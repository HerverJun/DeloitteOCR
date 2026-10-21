"""Server-owned business scope, independent of model and Harness setup."""
from dataclasses import dataclass, fields
from .identity import RunSuperseded


@dataclass(frozen=True)
class ToolContext:
    workspace_id: str
    session_id: str
    run_id: str
    generation: int
    turn_id: int
    operation_id: str
    selected_assets: tuple[str, ...]
    services: object
    store: object
    grants: frozenset[str] = frozenset({'read', 'derive'})

    def current(self):
        self.store.assert_current(self.run_id, self.generation)
        workspace = self.services.workspace_store.get_workspace(self.workspace_id)
        if workspace is None:
            raise ValueError('项目已不存在')

    def require_write(self):
        require_write(self)
        self.current()


BusinessContext = ToolContext


@dataclass(frozen=True)
class SessionContext(ToolContext):
    """A scoped product action can read saved results without an active run."""

    def current(self):
        if self.run_id:
            return super().current()
        if self.services.workspace_store.get_workspace(self.workspace_id) is None:
            raise ValueError('项目已不存在')
        if self.session_id and self.store.session(self.session_id)['workspace_id'] != self.workspace_id:
            raise RunSuperseded('会话不属于当前项目')


def require_write(context):
    if not getattr(context, 'grants', frozenset({'read', 'derive'})).intersection({'derive', 'versioned', 'control'}):
        raise ValueError('只读任务快照不允许提交或控制业务操作')


@dataclass(frozen=True)
class SnapshotContext(ToolContext):
    """An exact terminal run may be observed without regaining write authority."""

    def current(self):
        run = self.store.run(self.run_id)
        if (run['workspace_id'], run['session_id'], run['generation']) != (
                self.workspace_id, self.session_id, self.generation):
            raise RunSuperseded('任务快照代次或项目身份已变化')
        if self.services.workspace_store.get_workspace(self.workspace_id) is None:
            raise ValueError('项目已不存在')


def snapshot_context(context):
    values = {field.name: getattr(context, field.name) for field in fields(ToolContext)}
    return SnapshotContext(**(values | {'grants': frozenset({'read'}), 'operation_id': ''}))
