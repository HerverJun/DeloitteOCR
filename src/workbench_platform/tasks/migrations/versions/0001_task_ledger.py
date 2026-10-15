"""Initial central task ledger, identity and queue."""
from alembic import op
from workbench_platform.tasks.schema_v1 import metadata
revision = '0001_task_ledger'
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    metadata.create_all(op.get_bind())
    op.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')
    op.execute('CREATE INDEX tasks_search_name ON tasks USING gin (name gin_trgm_ops)')

def downgrade():
    raise RuntimeError('Destructive downgrade is disabled; restore the pre-upgrade backup into a new database')
