"""Pin native instance identity before crossing a versioned write boundary."""


def origin(ctx, application, link_id, instance_id):
    with ctx.store.transaction() as db:
        db.execute('CREATE TABLE IF NOT EXISTS native_operation_origins (operation_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, application TEXT NOT NULL, link_id TEXT NOT NULL, instance_id TEXT NOT NULL)')
        row = db.execute('SELECT * FROM native_operation_origins WHERE operation_id=?', (ctx.operation_id,)).fetchone()
        if row:
            if (row['workspace_id'], row['application'], row['link_id']) != (ctx.workspace_id, application, link_id):
                raise ValueError('原生操作身份变化')
            return row['instance_id']
        db.execute('INSERT INTO native_operation_origins VALUES(?,?,?,?,?)', (ctx.operation_id, ctx.workspace_id, application, link_id, instance_id))
        return instance_id
