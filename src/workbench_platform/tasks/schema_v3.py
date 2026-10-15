from sqlalchemy import MetaData,Table,Column,String,DateTime,Integer
metadata=MetaData()
workers=Table('task_workers',metadata,Column('id',String(100),primary_key=True),Column('hostname',String(200),nullable=False),
    Column('root_identity',String(64),nullable=False),Column('started_at',DateTime(timezone=True),nullable=False),
    Column('heartbeat_at',DateTime(timezone=True),nullable=False),Column('stopped_at',DateTime(timezone=True)),Column('concurrency',Integer,nullable=False))
