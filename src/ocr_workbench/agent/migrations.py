"""Business metadata only. Official saver schemas belong to a separate file."""


def migrate_v13(db):
    statements = [
        """CREATE TABLE ocr_submissions(
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, request_id TEXT NOT NULL,
            input_hash TEXT NOT NULL, task_ids TEXT NOT NULL, created TEXT NOT NULL,
            PRIMARY KEY(project_id,request_id))""",
        """CREATE TABLE agent_sessions(
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            client_request_id TEXT NOT NULL, request_hash TEXT NOT NULL,
            graph_thread_id TEXT NOT NULL UNIQUE, graph_version TEXT NOT NULL,
            title TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','archived')),
            next_seq INTEGER NOT NULL DEFAULT 1, created TEXT NOT NULL, updated TEXT NOT NULL,
            UNIQUE(project_id,client_request_id))""",
        "CREATE INDEX agent_sessions_project ON agent_sessions(project_id,updated,id)",
        """CREATE TABLE agent_runs(
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
            client_request_id TEXT NOT NULL, request_hash TEXT NOT NULL, goal TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued','running','waiting_jobs','waiting_user','completed','failed','cancelled','interrupted')),
            outcome TEXT CHECK(outcome IN ('success','partial','answered')), generation INTEGER NOT NULL DEFAULT 1,
            context TEXT NOT NULL, config TEXT NOT NULL, limits TEXT NOT NULL, usage TEXT NOT NULL DEFAULT '{}',
            coverage TEXT NOT NULL DEFAULT '{}', owner TEXT, fencing_token INTEGER NOT NULL DEFAULT 0,
            lease_expires REAL, last_event_seq INTEGER NOT NULL DEFAULT 0,
            created TEXT NOT NULL, updated TEXT NOT NULL, UNIQUE(session_id,client_request_id),
            CHECK((status='completed')=(outcome IS NOT NULL)))""",
        """CREATE UNIQUE INDEX agent_one_active_run ON agent_runs(session_id)
            WHERE status IN ('queued','running','waiting_jobs','waiting_user','interrupted')""",
        """CREATE TABLE agent_events(
            session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL, run_id TEXT REFERENCES agent_runs(id) ON DELETE CASCADE,
            generation INTEGER, event_key TEXT NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL,
            created TEXT NOT NULL, PRIMARY KEY(session_id,seq), UNIQUE(session_id,event_key))""",
        """CREATE TABLE agent_model_requests(
            request_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            step INTEGER NOT NULL, input_hash TEXT NOT NULL, generation INTEGER NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('sent','received','unknown','rejected')),
            normalized_response TEXT, usage TEXT, created TEXT NOT NULL, updated TEXT NOT NULL,
            UNIQUE(run_id,step))""",
        """CREATE TABLE agent_operations(
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            operation_key TEXT NOT NULL, input_hash TEXT NOT NULL, generation INTEGER NOT NULL,
            state TEXT NOT NULL, result TEXT, error TEXT, created TEXT NOT NULL, updated TEXT NOT NULL,
            UNIQUE(run_id,operation_key))""",
        """CREATE TABLE agent_calls(
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            step INTEGER NOT NULL, provider_call_id TEXT NOT NULL, tool TEXT NOT NULL, version INTEGER NOT NULL,
            canonical_args TEXT NOT NULL, state TEXT NOT NULL,
            operation_id TEXT REFERENCES agent_operations(id), result_ref TEXT,
            UNIQUE(run_id,step,provider_call_id))""",
        """CREATE TABLE agent_job_links(
            operation_id TEXT NOT NULL REFERENCES agent_operations(id) ON DELETE CASCADE,
            job_kind TEXT NOT NULL, job_id TEXT NOT NULL,
            ownership TEXT NOT NULL CHECK(ownership IN ('created','reused')), input_revision INTEGER,
            last_state TEXT NOT NULL, PRIMARY KEY(operation_id,job_kind,job_id))""",
        "CREATE INDEX agent_jobs_reverse ON agent_job_links(job_kind,job_id)",
        """CREATE TABLE agent_decisions(
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            kind TEXT NOT NULL, payload_hash TEXT NOT NULL, payload TEXT NOT NULL,
            scope TEXT NOT NULL, revision INTEGER, config_revision INTEGER,
            generation INTEGER NOT NULL, status TEXT NOT NULL, reply TEXT, expires REAL NOT NULL,
            client_request_id TEXT, created TEXT NOT NULL)""",
        """CREATE TABLE agent_grants(
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            run_id TEXT REFERENCES agent_runs(id) ON DELETE CASCADE,
            permission TEXT NOT NULL, scope_hash TEXT NOT NULL, scope TEXT NOT NULL,
            source TEXT NOT NULL CHECK(source IN ('user_request','bound_ui_scope','decision')),
            expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)""",
        "CREATE INDEX agent_grants_scope ON agent_grants(project_id,permission,scope_hash)",
        """CREATE TABLE agent_artifacts(
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            operation_id TEXT NOT NULL REFERENCES agent_operations(id) ON DELETE CASCADE,
            relative_path TEXT NOT NULL, sha256 TEXT, bytes INTEGER, mime TEXT NOT NULL,
            manifest TEXT NOT NULL, state TEXT NOT NULL, expires REAL NOT NULL,
            pinned INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL)""",
        "CREATE INDEX agent_artifacts_expiry ON agent_artifacts(pinned,state,expires)",
        """CREATE TABLE agent_artifact_leases(id TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL REFERENCES agent_artifacts(id) ON DELETE CASCADE, expires REAL NOT NULL)""",
        """CREATE TABLE agent_resume_intents(
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            generation INTEGER NOT NULL, checkpoint_id TEXT NOT NULL, interrupt_id TEXT NOT NULL,
            kind TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','claimed','applied','obsolete')),
            created TEXT NOT NULL, UNIQUE(run_id,generation,interrupt_id))""",
        """CREATE TABLE agent_inbox(
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            client_request_id TEXT NOT NULL, request_hash TEXT NOT NULL, content TEXT NOT NULL,
            context TEXT NOT NULL, state TEXT NOT NULL, created TEXT NOT NULL,
            UNIQUE(run_id,client_request_id))""",
        # Deletion is a durable outbox. Retain tombstones even when the project FK disappears.
        """CREATE TABLE agent_checkpoint_cleanup(
            thread_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, created TEXT NOT NULL)""",
        """CREATE TRIGGER agent_session_cleanup BEFORE DELETE ON agent_sessions BEGIN
            INSERT OR IGNORE INTO agent_checkpoint_cleanup VALUES(OLD.graph_thread_id,OLD.project_id,datetime('now'));
            END""",
        """CREATE TABLE agent_evidence(
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            reference TEXT NOT NULL, created TEXT NOT NULL)""",
        """CREATE TABLE agent_selections(id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            snapshot TEXT NOT NULL, expires REAL NOT NULL, created TEXT NOT NULL)""",
    ]
    for statement in statements:
        db.execute(statement)


def migrate_v14(db):
    db.execute("""CREATE TABLE agent_mutations(
        session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
        request_id TEXT NOT NULL, input_hash TEXT NOT NULL, action TEXT NOT NULL,
        run_id TEXT REFERENCES agent_runs(id) ON DELETE CASCADE,
        state TEXT NOT NULL CHECK(state IN ('pending','applied')), response TEXT,
        created TEXT NOT NULL, PRIMARY KEY(session_id,request_id))""")
