"""Explicit PostgreSQL configuration; startup never silently migrates or falls back."""
import os
from pathlib import Path
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from alembic.config import Config
from alembic import command

SCHEMA_REVISION = '0004_search_and_ai_filters'

def engine_for(url=None):
    url = make_url(url or os.environ['CLEAR_DATABASE_URL'])
    if url.drivername not in {'postgresql', 'postgresql+psycopg'}:
        raise ValueError('The central ledger requires PostgreSQL')
    if url.host not in {'127.0.0.1','localhost'}:
        raise ValueError('This installation requires PostgreSQL on the local F-drive server')
    return create_engine(url.set(drivername='postgresql+psycopg'), pool_pre_ping=True,
                         pool_size=10, max_overflow=15, connect_args={'connect_timeout': 10})

def check_schema(engine):
    with engine.connect() as db:
        if int(db.execute(text('SHOW server_version_num')).scalar_one()) < 170000:
            raise RuntimeError('PostgreSQL 17 or later is required')
        directory=f_path(db.execute(text('SHOW data_directory')).scalar_one())
        f_path(directory/'pg_wal')
        for location in db.execute(text("SELECT pg_tablespace_location(oid) FROM pg_tablespace WHERE spcname NOT IN ('pg_default','pg_global')")).scalars():
            if location:f_path(location)
        if db.execute(text("SELECT to_regclass('clear_alembic_version')")).scalar_one() is None:
            raise RuntimeError('Central database is not migrated; run tasks migrate')
        if db.execute(text('SELECT version_num FROM clear_alembic_version')).scalar_one() != SCHEMA_REVISION:
            raise RuntimeError('Unsupported central ledger revision')

def migrate(engine):
    config = Config()
    config.set_main_option('script_location', str(Path(__file__).with_name('migrations')))
    with engine.begin() as db:
        config.attributes['connection'] = db
        command.upgrade(config, 'head')

def f_path(value):
    requested = Path(value).absolute()
    actual = requested.resolve()
    if requested.drive.upper() != 'F:' or actual.drive.upper() != 'F:':
        raise ValueError('Project storage must remain on the local F drive')
    return actual
