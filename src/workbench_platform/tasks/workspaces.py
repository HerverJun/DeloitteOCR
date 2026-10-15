"""Existing WorkspaceStore interface over the authoritative central directory."""
from sqlalchemy import select, insert, update, delete, text
from workbench_platform.store.workspaces import Workspace, ModuleProjectLink, RevisionConflict, _id, _role, _text
from . import schema_v1 as s
from .store import key, now, wire, digest

class CentralWorkspaceStore:
    def __init__(self,engine): self.engine=engine
    @staticmethod
    def _workspace(row):
        return Workspace(row['workspace_id'],row['name'],row['revision'],wire(row['created_at']),wire(row['updated_at'])) if row else None
    @staticmethod
    def _link(row):
        return ModuleProjectLink(*(row[name] for name in ('workspace_id','app_id','module_project_id','link_id','role','revision')))
    def get_workspace(self,workspace_id):
        with self.engine.connect() as db: return self._workspace(db.execute(select(s.workspaces).where(s.workspaces.c.workspace_id==workspace_id)).mappings().first())
    def list_workspaces(self):
        with self.engine.connect() as db: return [self._workspace(r) for r in db.execute(select(s.workspaces).where(s.workspaces.c.archived.is_(False)).order_by(s.workspaces.c.workspace_id)).mappings()]
    def list_links(self,workspace_id):
        with self.engine.connect() as db: return [self._link(r) for r in db.execute(select(s.module_project_links).where(s.module_project_links.c.workspace_id==workspace_id).order_by(s.module_project_links.c.link_id)).mappings()]
    def create_workspace(self,name,*,workspace_id=None,request_id=None):
        name=_text(name,'workspace name'); workspace_id=_id(workspace_id or key('w'),'workspace id')
        with self.engine.begin() as db:
            if request_id:
                self._reserve(db,'workspace.create',request_id,name)
                previous=db.execute(select(s.audit_events.c.details).where(s.audit_events.c.action=='workspace.create.identity',s.audit_events.c.resource_id==request_id)).scalar_one_or_none()
                if previous:
                    workspace_id=previous['workspace_id']
                    return self._workspace(db.execute(select(s.workspaces).where(s.workspaces.c.workspace_id==workspace_id)).mappings().one())
            db.execute(insert(s.workspaces).values(workspace_id=workspace_id,name=name))
            if request_id: db.execute(insert(s.audit_events).values(action='workspace.create.identity',resource_id=request_id,details={'workspace_id':workspace_id}))
        return self.get_workspace(workspace_id)
    def _reserve(self,db,action,identity,payload):
        db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'),{'key':action+identity})
        row=db.execute(select(s.audit_events.c.details).where(s.audit_events.c.action==action,s.audit_events.c.resource_id==identity)).scalar_one_or_none()
        if row and row['hash']!=digest(payload): raise RevisionConflict('Creation request already used for different content')
        if not row: db.execute(insert(s.audit_events).values(action=action,resource_id=identity,details={'hash':digest(payload)}))
    def reserve_native_create(self,workspace_id,app_id,request_id,payload):
        import hashlib
        identity=f'{workspace_id}:{app_id}:{request_id}'
        with self.engine.begin() as db:self._reserve(db,'native.create.request',identity,payload)
        return 'p'+hashlib.sha256(identity.encode()).hexdigest()[:60]
    def _locked(self,db,workspace_id,expected_revision):
        row=db.execute(select(s.workspaces).where(s.workspaces.c.workspace_id==workspace_id).with_for_update()).mappings().first()
        if not row: raise KeyError('Workspace not found')
        if row['revision']!=expected_revision: raise RevisionConflict('Workspace revision changed')
        db.execute(update(s.workspaces).where(s.workspaces.c.workspace_id==workspace_id).values(revision=expected_revision+1,updated_at=now()))
    def rename_workspace(self,workspace_id,name,*,expected_revision):
        with self.engine.begin() as db:
            self._locked(db,workspace_id,expected_revision)
            db.execute(update(s.workspaces).where(s.workspaces.c.workspace_id==workspace_id).values(name=_text(name,'workspace name')))
        return self.get_workspace(workspace_id)
    def add_link(self,workspace_id,app_id,module_project_id,*,role,expected_revision,link_id=None):
        row=dict(workspace_id=workspace_id,app_id=app_id,module_project_id=module_project_id,link_id=link_id or key('l'),role=_role(role),revision=1)
        with self.engine.begin() as db:
            self._locked(db,workspace_id,expected_revision)
            db.execute(insert(s.module_project_links).values(**row))
        return self._link(row)
    def update_link_role(self,workspace_id,link_id,role,*,expected_revision,expected_link_revision):
        with self.engine.begin() as db:
            self._locked(db,workspace_id,expected_revision)
            row=db.execute(select(s.module_project_links).where(s.module_project_links.c.workspace_id==workspace_id,s.module_project_links.c.link_id==link_id)).mappings().first()
            if not row: raise KeyError('Project link not found')
            if row['revision']!=expected_link_revision: raise RevisionConflict('Link revision changed')
            row={**row,'role':_role(role),'revision':expected_link_revision+1}
            db.execute(update(s.module_project_links).where(s.module_project_links.c.link_id==link_id).values(role=row['role'],revision=row['revision']))
        return self._link(row)
    def remove_link(self,workspace_id,link_id,*,expected_revision):
        with self.engine.begin() as db:
            self._locked(db,workspace_id,expected_revision)
            db.execute(delete(s.module_project_links).where(s.module_project_links.c.workspace_id==workspace_id,s.module_project_links.c.link_id==link_id))
