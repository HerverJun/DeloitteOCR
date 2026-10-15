"""Index the actual NULLS LAST page order and track idle execution hosts."""
from alembic import op
from workbench_platform.tasks.schema_v3 import workers
revision='0003_query_order_and_workers'
down_revision='0002_legacy_unknown_times'
branch_labels=None
depends_on=None
def upgrade():
    workers.create(op.get_bind())
    for name,columns in [('tasks_workspace_created','workspace_id,created_at DESC NULLS LAST,id DESC'),
        ('tasks_creator_created','created_by,created_at DESC NULLS LAST,id DESC'),
        ('tasks_status_created','workspace_id,status,created_at DESC NULLS LAST,id DESC'),
        ('tasks_tool_created','workspace_id,tool_id,created_at DESC NULLS LAST,id DESC')]:
        op.execute(f'DROP INDEX {name}')
        op.execute(f'CREATE INDEX {name} ON tasks({columns})')
    op.execute('DROP INDEX tasks_roots_created')
    op.execute('CREATE INDEX tasks_roots_created ON tasks(created_at DESC NULLS LAST,id DESC) WHERE parent_task_id IS NULL')
def downgrade():raise RuntimeError('Restore the pre-upgrade backup to a new database')
