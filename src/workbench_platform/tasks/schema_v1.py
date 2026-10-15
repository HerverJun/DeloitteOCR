"""Frozen PostgreSQL schema for central ledger revision 0001."""
from sqlalchemy import (MetaData, Table, Column as C, String as S, Text, Integer as I,
                        BigInteger as B, Boolean, DateTime as DT, Numeric, ForeignKey as FK,
                        UniqueConstraint as U, CheckConstraint as Check, Index, Sequence, text)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()
stamp = lambda name: C(name, DT(timezone=True), nullable=False, server_default=text('CURRENT_TIMESTAMP'))
optional_stamp = lambda name: C(name, DT(timezone=True))
identifier = lambda name='id': C(name, S(80), primary_key=True)
json = lambda name, default='{}': C(name, JSONB, nullable=False, server_default=text(f"'{default}'::jsonb"))
task_numbers = Sequence('clear_task_numbers', metadata=metadata)

users = Table('users', metadata, identifier(), C('username', S(100), nullable=False, unique=True),
    C('display_name', S(200), nullable=False), C('password_hash', Text, nullable=False),
    C('is_admin', Boolean, nullable=False, server_default=text('false')),
    C('disabled', Boolean, nullable=False, server_default=text('false')), stamp('created_at'))
user_sessions = Table('user_sessions', metadata, identifier(), C('token_hash', S(64), nullable=False, unique=True),
    C('user_id', FK('users.id'), nullable=False), C('csrf_hash', S(64), nullable=False),
    stamp('created_at'), optional_stamp('expires_at'))
workspaces = Table('workspaces', metadata, identifier('workspace_id'), C('name', S(200), nullable=False),
    C('revision', I, nullable=False, server_default='1'), stamp('created_at'), stamp('updated_at'),
    C('archived', Boolean, nullable=False, server_default=text('false')))
workspace_members = Table('workspace_members', metadata,
    C('workspace_id', FK('workspaces.workspace_id'), primary_key=True),
    C('user_id', FK('users.id'), primary_key=True), C('role', S(20), nullable=False),
    Check("role IN ('owner','editor','viewer')", name='member_role'))
module_project_links = Table('module_project_links', metadata, identifier('link_id'),
    C('workspace_id', FK('workspaces.workspace_id'), nullable=False), C('app_id', S(80), nullable=False),
    C('module_project_id', S(100), nullable=False), C('role', S(20), nullable=False),
    C('revision', I, nullable=False, server_default='1'),
    U('workspace_id','app_id','module_project_id', name='module_link_identity'))
ai_service_profiles = Table('ai_service_profiles', metadata, identifier(), C('name', S(200), nullable=False),
    C('provider', S(100), nullable=False), C('model', S(200)), C('credential_ref', Text),
    json('configuration'), C('enabled', Boolean, nullable=False, server_default=text('true')), stamp('created_at'))
tasks = Table('tasks', metadata, identifier(), C('task_code', S(80), nullable=False, unique=True),
    C('workspace_id', FK('workspaces.workspace_id')), C('project_name_snapshot', S(200)),
    C('created_by', FK('users.id')), C('creator_name_snapshot', S(200)),
    C('name', S(200), nullable=False), C('description', Text, nullable=False, server_default=''),
    C('tool_id', S(80), nullable=False), C('action', S(100), nullable=False), C('tool_version', S(100)),
    C('source', S(30), nullable=False), C('parent_task_id', FK('tasks.id')), C('parent_run_id', S(80)),
    C('status', S(30), nullable=False), C('stage', S(256), nullable=False, server_default=''),
    C('availability', S(30), nullable=False, server_default='not_ready'), C('native_status', S(256)),
    C('current_run_id', S(80)), C('run_count', I, nullable=False, server_default='1'),
    C('revision', I, nullable=False, server_default='1'), C('input_total_bytes', B), C('file_count', I),
    C('page_count', I), C('progress_done', B), C('progress_total', B),
    C('idempotency_key', S(200), nullable=False), C('request_hash', S(64), nullable=False),
    json('options'), json('summary'), stamp('created_at'), stamp('updated_at'),
    optional_stamp('last_verified_at'), C('archived', Boolean, nullable=False, server_default=text('false')),
    U('created_by','idempotency_key', name='task_submission_identity'),
    Check('input_total_bytes IS NULL OR input_total_bytes >= 0', name='task_nonnegative_bytes'))
task_inputs = Table('task_inputs', metadata, identifier(), C('task_id', FK('tasks.id'), nullable=False),
    C('asset_id', S(100)), C('asset_revision', I), C('blob_hash', S(64)), C('name', S(300)),
    C('size_bytes', B), json('source_locator'), U('task_id','asset_id','asset_revision', name='task_input_identity'))
task_runs = Table('task_runs', metadata, identifier(), C('task_id', FK('tasks.id'), nullable=False),
    C('attempt', I, nullable=False), C('status', S(30), nullable=False), C('generation', I, nullable=False),
    C('requested_by', FK('users.id')), json('request_snapshot'), stamp('queued_at'),
    optional_stamp('started_at'), optional_stamp('finished_at'), C('elapsed_ms', B), C('queue_ms', B),
    C('error', Text), json('scope'), U('task_id','attempt', name='run_attempt_identity'))
task_step_runs = Table('task_step_runs', metadata, identifier(), C('task_id', FK('tasks.id'), nullable=False),
    C('run_id', FK('task_runs.id'), nullable=False), C('input_id', FK('task_inputs.id')),
    C('step_key', S(200), nullable=False), C('attempt', I, nullable=False), C('tool_id', S(80), nullable=False),
    C('action', S(100), nullable=False), C('status', S(30), nullable=False),
    C('native_id', S(200)), C('native_generation', I), C('worker_id', S(100)),
    C('reused_from_step_run_id', FK('task_step_runs.id')), optional_stamp('started_at'),
    optional_stamp('finished_at'), C('elapsed_ms', B), C('error', Text), json('details'),
    U('run_id','step_key','attempt', name='step_attempt_identity'))
task_events = Table('task_events', metadata, C('seq', B, primary_key=True, autoincrement=True),
    C('task_id', FK('tasks.id'), nullable=False), C('run_id', FK('task_runs.id')),
    C('identity', S(200), nullable=False, unique=True), C('kind', S(100), nullable=False),
    C('actor_id', FK('users.id')), json('payload'), stamp('created_at'))
ai_calls = Table('ai_calls', metadata, identifier(), C('task_id', FK('tasks.id'), nullable=False),
    C('run_id', FK('task_runs.id'), nullable=False), C('step_run_id', FK('task_step_runs.id')),
    C('profile_id', FK('ai_service_profiles.id')), C('connection_name', S(200)),
    C('provider', S(100)), C('requested_model', S(200)), C('actual_model', S(200)),
    C('input_tokens', B), C('output_tokens', B), C('cost', Numeric(20,8)), C('currency', S(8)),
    C('status', S(30), nullable=False), C('elapsed_ms', B), json('metadata'), stamp('created_at'))
task_artifact_links = Table('task_artifact_links', metadata, identifier(), C('task_id', FK('tasks.id'), nullable=False),
    C('run_id', FK('task_runs.id'), nullable=False), C('artifact_id', S(200)), C('name', S(300), nullable=False),
    C('kind', S(30), nullable=False), C('blob_hash', S(64)), C('size_bytes', B), C('source_path', Text),
    C('download_route', Text), C('missing', Boolean, nullable=False, server_default=text('false')), stamp('created_at'))
task_dispatches = Table('task_dispatches', metadata, identifier(), C('task_id', FK('tasks.id'), nullable=False),
    C('run_id', FK('task_runs.id'), nullable=False), C('kind', S(30), nullable=False),
    C('state', S(30), nullable=False), C('worker_id', S(100)), C('lease_token', S(80)),
    optional_stamp('lease_until'), optional_stamp('heartbeat_at'), C('generation', I, nullable=False),
    json('command'), json('receipt'), stamp('created_at'), stamp('updated_at'))
task_source_links = Table('task_source_links', metadata, identifier(), C('task_id', FK('tasks.id'), nullable=False),
    C('run_id', FK('task_runs.id')), C('namespace', S(100), nullable=False), C('source_id', S(300), nullable=False),
    C('source_generation', I, nullable=False, server_default='1'), json('metadata'),
    U('namespace','source_id','source_generation', name='source_record_identity'))
audit_events = Table('audit_events', metadata, C('seq', B, primary_key=True, autoincrement=True),
    C('actor_id', FK('users.id')), C('workspace_id', FK('workspaces.workspace_id')),
    C('action', S(100), nullable=False), C('resource_id', S(300)), json('details'), stamp('created_at'))
control_requests = Table('task_control_requests', metadata, identifier(), C('task_id', FK('tasks.id'), nullable=False),
    C('actor_id', FK('users.id'), nullable=False), C('request_key', S(200), nullable=False),
    C('request_hash', S(64), nullable=False), json('receipt'), U('actor_id','request_key',name='control_identity'))

Index('tasks_workspace_created', tasks.c.workspace_id, tasks.c.created_at.desc(), tasks.c.id.desc())
Index('tasks_creator_created', tasks.c.created_by, tasks.c.created_at.desc(), tasks.c.id.desc())
Index('tasks_status_created', tasks.c.workspace_id, tasks.c.status, tasks.c.created_at.desc(), tasks.c.id.desc())
Index('tasks_tool_created', tasks.c.workspace_id, tasks.c.tool_id, tasks.c.created_at.desc(), tasks.c.id.desc())
Index('tasks_roots_created', tasks.c.created_at.desc(), tasks.c.id.desc(), postgresql_where=tasks.c.parent_task_id.is_(None))
Index('task_events_cursor', task_events.c.task_id, task_events.c.seq)
Index('task_runs_task', task_runs.c.task_id, task_runs.c.attempt)
Index('dispatch_ready', task_dispatches.c.created_at, postgresql_where=task_dispatches.c.state == 'queued')
Index('dispatch_lease', task_dispatches.c.lease_until, postgresql_where=task_dispatches.c.state == 'leased')
Index('ai_calls_task', ai_calls.c.task_id, ai_calls.c.created_at)
Index('artifact_task', task_artifact_links.c.task_id, task_artifact_links.c.run_id)
