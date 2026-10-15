from alembic import context
from workbench_platform.tasks.schema_v1 import metadata
connection = context.config.attributes['connection']
context.configure(connection=connection, target_metadata=metadata,
                  version_table='clear_alembic_version', transaction_per_migration=True)
with context.begin_transaction():
    context.run_migrations()
