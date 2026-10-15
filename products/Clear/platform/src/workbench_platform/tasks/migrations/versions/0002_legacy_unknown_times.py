"""Keep unrecorded historical timestamps unknown instead of inventing them."""
from alembic import op
revision='0002_legacy_unknown_times'
down_revision='0001_task_ledger'
branch_labels=None
depends_on=None
def upgrade():
    op.alter_column('tasks','created_at',nullable=True)
    op.alter_column('task_runs','queued_at',nullable=True)
def downgrade():
    raise RuntimeError('Restore a pre-upgrade backup instead of fabricating unknown timestamps')
