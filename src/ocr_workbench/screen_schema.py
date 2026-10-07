"""Transactional v18 migration for photographed-table batch state."""

SCREEN_SCHEMA = """
CREATE TABLE IF NOT EXISTS screen_batches(
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  request_id TEXT NOT NULL, payload_hash TEXT NOT NULL, policy TEXT NOT NULL,
  status TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL,
  UNIQUE(project_id,request_id));
CREATE TABLE IF NOT EXISTS screen_items(
  batch_id TEXT NOT NULL REFERENCES screen_batches(id) ON DELETE CASCADE,
  input_version_id TEXT NOT NULL REFERENCES versions(id), image_id TEXT NOT NULL,
  position INTEGER NOT NULL, source_hash TEXT NOT NULL, source_name TEXT NOT NULL,
  status TEXT NOT NULL, stage TEXT NOT NULL, preparation TEXT, candidates TEXT NOT NULL DEFAULT '[]',
  issues TEXT NOT NULL DEFAULT '[]', pass_index INTEGER NOT NULL DEFAULT 0,
  attempt INTEGER NOT NULL DEFAULT 0,
  result_id TEXT, result_revision INTEGER, review_state TEXT NOT NULL DEFAULT 'automatic',
  error TEXT, started TEXT, updated TEXT NOT NULL,
  PRIMARY KEY(batch_id,input_version_id));
CREATE TABLE IF NOT EXISTS screen_tasks(
  task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
  batch_id TEXT NOT NULL REFERENCES screen_batches(id) ON DELETE CASCADE,
  input_version_id TEXT NOT NULL, pass_index INTEGER NOT NULL, prepared_id TEXT NOT NULL,
  prepared_path TEXT NOT NULL, metadata TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
  UNIQUE(batch_id,input_version_id,pass_index,prepared_id,attempt));
CREATE INDEX IF NOT EXISTS screen_items_state ON screen_items(batch_id,status,position);
CREATE TABLE IF NOT EXISTS screen_final_tasks(
  batch_id TEXT NOT NULL REFERENCES screen_batches(id) ON DELETE CASCADE,
  input_version_id TEXT NOT NULL, content_hash TEXT NOT NULL, task_id TEXT NOT NULL,
  PRIMARY KEY(batch_id,input_version_id,content_hash));
CREATE TABLE IF NOT EXISTS screen_actions(
  batch_id TEXT NOT NULL REFERENCES screen_batches(id) ON DELETE CASCADE,
  request_id TEXT NOT NULL, payload_hash TEXT NOT NULL, receipt TEXT NOT NULL,
  PRIMARY KEY(batch_id,request_id));
CREATE TABLE IF NOT EXISTS screen_contexts(
  task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
  batch_id TEXT NOT NULL REFERENCES screen_batches(id) ON DELETE CASCADE,
  input_version_id TEXT NOT NULL, raw TEXT, artifact TEXT,
  UNIQUE(batch_id,input_version_id));
CREATE TABLE IF NOT EXISTS screen_cache(
  cache_key TEXT PRIMARY KEY, result_id TEXT NOT NULL REFERENCES results(id) ON DELETE CASCADE,
  created TEXT NOT NULL);
"""

def migrate_screen_schema(db):
    for statement in SCREEN_SCHEMA.split(';'):
        if statement.strip():
            db.execute(statement)
    columns = {row['name'] for row in db.execute('PRAGMA table_info(screen_batches)')}
    if 'parent_batch_id' not in columns:
        db.execute('ALTER TABLE screen_batches ADD COLUMN parent_batch_id TEXT')
    for column in ('deadline', 'paused_at'):
        if column not in columns:
            db.execute('ALTER TABLE screen_batches ADD COLUMN ' + column + ' REAL')
    columns = {row['name'] for row in db.execute('PRAGMA table_info(screen_items)')}
    if 'evidence' not in columns:
        db.execute('ALTER TABLE screen_items ADD COLUMN evidence TEXT')
