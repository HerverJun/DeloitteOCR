"""Both branches of keyword search and AI service filters need indexes."""
from alembic import op
revision='0004_search_and_ai_filters'
down_revision='0003_query_order_and_workers'
branch_labels=None
depends_on=None
def upgrade():
    op.execute('CREATE INDEX tasks_search_code ON tasks USING gin (task_code gin_trgm_ops)')
    op.execute('CREATE INDEX ai_calls_profile_task ON ai_calls(profile_id,task_id)')
    op.execute('CREATE INDEX ai_calls_provider_task ON ai_calls(provider,task_id)')
def downgrade():raise RuntimeError('Restore the pre-upgrade backup to a new database')
