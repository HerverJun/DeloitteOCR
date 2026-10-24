"""Both former v18 layouts migrate without losing native project state."""
from contextlib import closing
from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest

from ocr_workbench.store import Store, SCHEMA_VERSION, _migrate_engine_routes
from ocr_workbench.screen_schema import migrate_screen_schema
from ocr_workbench.screen_local_candidates import migrate_local_candidates
from ocr_workbench.screen_cell_review import migrate_cell_reviews


@pytest.mark.parametrize('layout,version', [('mineru', 18), ('screen', 18), ('screen', 21)])
def test_old_layouts_upgrade_with_preserved_project_and_backup(tmp_path, layout, version):
    folder = tmp_path / (layout + str(version))
    with patch('ocr_workbench.store.SCHEMA_VERSION', 17):
        old = Store(folder)
        project = old.project('保留编号 00123 与金额 100.20')
    with closing(sqlite3.connect(folder / 'workbench.sqlite3')) as db:
        db.row_factory = sqlite3.Row
        if layout == 'mineru':
            _migrate_engine_routes(db)
        else:
            migrate_screen_schema(db)
            if version >= 19:
                migrate_local_candidates(db)
                migrate_cell_reviews(db)
        db.execute('PRAGMA user_version=' + str(version))
        db.commit()
    upgraded = Store(folder)
    assert upgraded.schema_version == SCHEMA_VERSION == 22
    assert upgraded.one('projects', project['id'])['name'] == project['name']
    names = {row['name'] for row in upgraded.rows("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'screen_batches', 'screen_local_requests', 'screen_cell_groups', 'task_engine_routes'} <= names
    assert upgraded.rows('PRAGMA foreign_key_check') == []
    assert upgraded.rows('PRAGMA integrity_check')[0]['integrity_check'] == 'ok'
    with closing(sqlite3.connect(upgraded.migration_backup)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == version
        assert db.execute('SELECT name FROM projects WHERE id=?', (project['id'],)).fetchone()[0] == project['name']


def test_mineru_layout_upgrade_rolls_back_both_schema_and_screen_tables(tmp_path):
    with patch('ocr_workbench.store.SCHEMA_VERSION', 17):
        old = Store(tmp_path)
        project = old.project('迁移失败保留')
    with closing(sqlite3.connect(tmp_path / 'workbench.sqlite3')) as db:
        _migrate_engine_routes(db)
        db.execute('PRAGMA user_version=18')
        db.commit()
    with patch('ocr_workbench.screen_cell_review.migrate_cell_reviews', side_effect=RuntimeError('controlled failure')):
        with pytest.raises(RuntimeError, match='controlled failure'):
            Store(tmp_path)
    with closing(sqlite3.connect(tmp_path / 'workbench.sqlite3')) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 18
        assert db.execute("SELECT name FROM sqlite_master WHERE name='screen_batches'").fetchone() is None
        assert db.execute('SELECT name FROM projects WHERE id=?', (project['id'],)).fetchone()[0] == project['name']
