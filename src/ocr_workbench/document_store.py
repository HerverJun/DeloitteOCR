"""Schema 9 document relations, version-bound regions and durable CPU stages."""

import hashlib
import json

from ocr_workbench.coordinates import IDENTITY, multiply, operation_transform, validate_polygon


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def structure_fingerprint(edit):
    """Text-only edits preserve geometry. Cell topology/order is part of identity."""
    return fingerprint([{"rows": t["rows"], "columns": t["columns"],
                         "cells": [[c["row"], c["column"], c.get("row_span", 1),
                                    c.get("column_span", 1)] for c in t["cells"]]}
                        for t in edit.get("tables", [])])


def migrate_v9(db):
    # Keep the migration inside Store's existing BEGIN IMMEDIATE, with no
    # executescript (which would implicitly commit the old schema transaction).
    statements = [
        """CREATE TABLE documents(id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            name TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('image','pdf','tiff')),
            original_path TEXT NOT NULL,sha256 TEXT NOT NULL,page_count INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'ready',metadata TEXT NOT NULL DEFAULT '{}',
            created TEXT NOT NULL,updated TEXT NOT NULL)""",
        """CREATE TABLE pages(id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            page_number INTEGER NOT NULL CHECK(page_number>0),
            image_id TEXT UNIQUE REFERENCES images(id) ON DELETE SET NULL,
            width_points REAL,height_points REAL,crop_box TEXT,rotation INTEGER NOT NULL DEFAULT 0,
            render_dpi REAL NOT NULL DEFAULT 300,render_parameters TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',native_result TEXT,
            created TEXT NOT NULL,updated TEXT NOT NULL,UNIQUE(document_id,page_number))""",
        """CREATE TABLE page_versions(version_id TEXT PRIMARY KEY REFERENCES versions(id) ON DELETE CASCADE,
            page_id TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
            pdf_to_pixel TEXT,parent_to_pixel TEXT,
            mapping_status TEXT NOT NULL CHECK(mapping_status IN ('exact','nonlinear','image')),
            created TEXT NOT NULL)""",
        """CREATE TABLE regions(id TEXT PRIMARY KEY,
            page_id TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
            version_id TEXT NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,reading_order INTEGER NOT NULL,source TEXT NOT NULL,
            polygon TEXT NOT NULL,pdf_polygon TEXT,metadata TEXT NOT NULL DEFAULT '{}',created TEXT NOT NULL)""",
        """CREATE TABLE geometry_evidence(id TEXT PRIMARY KEY,
            result_id TEXT NOT NULL REFERENCES results(id) ON DELETE CASCADE,
            version_id TEXT NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
            region_id TEXT REFERENCES regions(id) ON DELETE SET NULL,
            image_sha256 TEXT NOT NULL,structure_sha256 TEXT NOT NULL,
            source TEXT NOT NULL,model_version TEXT NOT NULL,
            target TEXT NOT NULL,polygon TEXT,details TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'valid',created TEXT NOT NULL)""",
        """CREATE TABLE document_stages(id TEXT PRIMARY KEY,
            page_id TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
            version_id TEXT REFERENCES versions(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,request_key TEXT NOT NULL,parameters TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',phase TEXT NOT NULL DEFAULT '等待处理',
            attempt INTEGER NOT NULL DEFAULT 0,error TEXT,output TEXT,
            created TEXT NOT NULL,started TEXT,finished TEXT,
            UNIQUE(page_id,kind,request_key))""",
        """CREATE TABLE geometry_requests(task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            result_id TEXT NOT NULL REFERENCES results(id) ON DELETE CASCADE,
            snapshot TEXT NOT NULL,structure_sha256 TEXT NOT NULL,regions TEXT NOT NULL)""",
        """CREATE TABLE page_ocr_inputs(task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            stage_id TEXT NOT NULL REFERENCES document_stages(id) ON DELETE CASCADE,
            region_id TEXT REFERENCES regions(id) ON DELETE SET NULL,crop_box TEXT NOT NULL,
            output TEXT,UNIQUE(stage_id,region_id))""",
        "CREATE INDEX page_ocr_stage ON page_ocr_inputs(stage_id)",
        """CREATE TABLE document_conflict_decisions(result_id TEXT NOT NULL REFERENCES results(id) ON DELETE CASCADE,
            conflict_id TEXT NOT NULL,revision INTEGER NOT NULL,edited_sha256 TEXT NOT NULL,created TEXT NOT NULL,
            PRIMARY KEY(result_id,conflict_id))""",
        """CREATE TABLE review_timings(id TEXT PRIMARY KEY,
            result_id TEXT NOT NULL REFERENCES results(id) ON DELETE CASCADE,
            revision INTEGER NOT NULL,target TEXT NOT NULL,active_ms INTEGER NOT NULL,
            action TEXT NOT NULL,created TEXT NOT NULL)""",
        "CREATE INDEX documents_project ON documents(project_id,created,id)",
        "CREATE INDEX pages_document ON pages(document_id,page_number)",
        "CREATE INDEX regions_version ON regions(version_id,reading_order)",
        "CREATE INDEX geometry_result ON geometry_evidence(result_id,version_id,status)",
        "CREATE INDEX geometry_target ON geometry_evidence(result_id,version_id,status,target)",
        "CREATE INDEX document_stages_queue ON document_stages(status,created)",
        """INSERT INTO documents(id,project_id,name,kind,original_path,sha256,page_count,created,updated)
            SELECT id,project_id,name,'image',original_path,sha256,1,created,created FROM images""",
        """INSERT INTO pages(id,document_id,page_number,image_id,status,created,updated)
            SELECT id,id,1,id,'ready',created,created FROM images""",
        """INSERT INTO page_versions(version_id,page_id,mapping_status,created)
            SELECT id,image_id,'image',created FROM versions""",
        """CREATE TRIGGER page_image_project BEFORE UPDATE OF image_id ON pages
            WHEN NEW.image_id IS NOT NULL AND
              (SELECT project_id FROM images WHERE id=NEW.image_id) IS NOT
              (SELECT project_id FROM documents WHERE id=NEW.document_id)
            BEGIN SELECT RAISE(ABORT,'Page image belongs to another project'); END""",
        """CREATE TRIGGER page_version_owner BEFORE INSERT ON page_versions
            WHEN (SELECT image_id FROM versions WHERE id=NEW.version_id) IS NOT
                 (SELECT image_id FROM pages WHERE id=NEW.page_id)
            BEGIN SELECT RAISE(ABORT,'Page version belongs to another image'); END""",
        """CREATE TRIGGER region_version_owner BEFORE INSERT ON regions
            WHEN NEW.page_id IS NOT (SELECT page_id FROM page_versions WHERE version_id=NEW.version_id)
            BEGIN SELECT RAISE(ABORT,'Region version belongs to another page'); END""",
    ]
    for statement in statements:
        db.execute(statement)
    for table in ("documents", "pages", "regions", "document_stages", "geometry_evidence"):
        for action in ("INSERT", "UPDATE", "DELETE"):
            ref = "OLD" if action == "DELETE" else "NEW"
            if table == "documents":
                project = f"{ref}.project_id"
            elif table == "pages":
                project = f"(SELECT project_id FROM documents WHERE id={ref}.document_id)"
            elif table == "geometry_evidence":
                project = f"(SELECT t.project_id FROM tasks t JOIN results r ON r.task_id=t.id WHERE r.id={ref}.result_id)"
            else:
                project = f"(SELECT d.project_id FROM documents d JOIN pages p ON p.document_id=d.id WHERE p.id={ref}.page_id)"
            db.execute(f"CREATE TRIGGER revision_{table}_{action.lower()} AFTER {action} ON {table} "
                       f"BEGIN UPDATE project_revisions SET revision=revision+1 WHERE project_id={project}; END")


class DocumentStoreMixin:
    def link_image_document(self, db, image_id, *, page_id=None, pdf_to_pixel=None):
        from ocr_workbench.store import encoded, now
        image = db.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
        version_id = image["active_version"]
        if page_id is None:
            page_id = image_id
            db.execute("""INSERT INTO documents(id,project_id,name,kind,original_path,sha256,page_count,created,updated)
                VALUES(?,?,?,'image',?,?,1,?,?)""",
                       (image_id, image["project_id"], image["name"], image["original_path"],
                        image["sha256"], image["created"], now()))
            db.execute("""INSERT INTO pages(id,document_id,page_number,image_id,status,created,updated)
                VALUES(?,?,1,?,'ready',?,?)""", (page_id, image_id, image_id, now(), now()))
        else:
            page = db.execute("SELECT * FROM pages WHERE id=?", (page_id,)).fetchone()
            if page is None:
                raise KeyError("页面不存在")
            if page["image_id"]:
                raise ValueError("页面已展开，请复用已有图像版本")
            db.execute("UPDATE pages SET image_id=?,status='ready',updated=? WHERE id=?", (image_id, now(), page_id))
        db.execute("INSERT INTO page_versions VALUES(?,?,?,?,?,?)",
                   (version_id, page_id, encoded(pdf_to_pixel) if pdf_to_pixel else None,
                    encoded(IDENTITY), "exact" if pdf_to_pixel else "image", now()))

    def link_derived_version(self, db, parent, version_id, operation):
        from ocr_workbench.store import encoded, now
        relation = db.execute("SELECT * FROM page_versions WHERE version_id=?", (parent["id"],)).fetchone()
        if relation is None:
            return
        transform = operation_transform(operation, parent["width"], parent["height"])
        pdf = (multiply(transform, json.loads(relation["pdf_to_pixel"]))
               if transform is not None and relation["pdf_to_pixel"] else None)
        status = "nonlinear" if transform is None or relation["mapping_status"] == "nonlinear" else relation["mapping_status"]
        db.execute("INSERT INTO page_versions VALUES(?,?,?,?,?,?)",
                   (version_id, relation["page_id"], encoded(pdf) if pdf else None,
                    encoded(transform) if transform is not None else None, status, now()))

    def document_pages(self, document_id, offset=0, limit=50):
        self.one("documents", document_id)
        if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 200:
            raise ValueError("分页范围无效；每批最多 200 页")
        return self.rows("""SELECT p.*,i.active_version,s.result_id selected_result,
            (SELECT status FROM document_stages WHERE page_id=p.id ORDER BY created DESC,id DESC LIMIT 1) stage_status,
            (SELECT error FROM document_stages WHERE page_id=p.id ORDER BY created DESC,id DESC LIMIT 1) stage_error,
            (SELECT phase FROM document_stages WHERE page_id=p.id ORDER BY created DESC,id DESC LIMIT 1) stage_phase
            FROM pages p LEFT JOIN images i ON i.id=p.image_id LEFT JOIN selections s ON s.image_id=p.image_id
            WHERE p.document_id=? ORDER BY p.page_number LIMIT ? OFFSET ?""", (document_id, limit, offset))

    def page_for_version(self, version_id):
        rows = self.rows("SELECT p.*,pv.pdf_to_pixel,pv.mapping_status FROM pages p JOIN page_versions pv ON pv.page_id=p.id WHERE pv.version_id=?", (version_id,))
        if not rows:
            raise KeyError("图像版本未关联页面")
        return rows[0]

    def add_region(self, page_id, version_id, kind, polygon, source, *, reading_order=0, metadata=None, db=None):
        from ocr_workbench.store import uid, now, encoded
        from ocr_workbench.coordinates import inverse, polygon as map_polygon
        if db is None:
            with self.transaction() as connection:
                return self.add_region(page_id, version_id, kind, polygon, source,
                                       reading_order=reading_order, metadata=metadata, db=connection)
        version = db.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
        if version is None:
            raise KeyError("图像版本不存在")
        validate_polygon(polygon, version["width"], version["height"])
        relation = db.execute("SELECT * FROM page_versions WHERE version_id=?", (version_id,)).fetchone()
        if relation is None or relation["page_id"] != page_id:
            raise ValueError("区域不属于该页面版本")
        pdf_polygon = (map_polygon(inverse(json.loads(relation["pdf_to_pixel"])), polygon)
                       if relation["pdf_to_pixel"] else None)
        key = uid()
        db.execute("INSERT INTO regions VALUES(?,?,?,?,?,?,?,?,?,?)", (key, page_id, version_id, kind,
                   reading_order, source, encoded(polygon), encoded(pdf_polygon) if pdf_polygon else None,
                   encoded(metadata or {}), now()))
        return key

    def page_regions(self, page_id, version_id=None):
        page = self.one("pages", page_id)
        if version_id is None and page["image_id"]:
            version_id = self.one("images", page["image_id"])["active_version"]
        if version_id and self.page_for_version(version_id)["id"] != page_id:
            raise ValueError("版本不属于该页面")
        rows = self.rows("SELECT * FROM regions WHERE page_id=? AND version_id=? ORDER BY reading_order,id", (page_id, version_id))
        for row in rows:
            for name in ("polygon", "pdf_polygon", "metadata"):
                row[name] = json.loads(row[name]) if row[name] is not None else None
        return rows

    def enqueue_document_stage(self, page_id, kind, parameters, *, force=False):
        from ocr_workbench.store import uid, now, encoded, Conflict
        if kind not in ("render", "process", "table_structure"):
            raise ValueError("未知文档处理阶段")
        if type(force) is not bool:
            raise ValueError("强制重新处理选项必须是布尔值")
        # Secrets only live in the CPU manager's in-memory vault.
        if any("password" in str(key).lower() for key in parameters):
            raise ValueError("密码不能写入处理参数")
        with self.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            page = db.execute("SELECT * FROM pages WHERE id=?", (page_id,)).fetchone()
            if not page:
                raise KeyError("页面不存在")
            active = db.execute("SELECT active_version FROM images WHERE id=?", (page["image_id"],)).fetchone()
            version_id = active[0] if active else None
            key_hash = fingerprint({"kind": kind, "parameters": parameters, "version_id": version_id})
            pending = db.execute("SELECT id FROM document_stages WHERE page_id=? AND status IN ('queued','running','waiting_gpu')", (page_id,)).fetchone()
            if pending:
                return pending["id"]
            prior = db.execute("SELECT id,status FROM document_stages WHERE page_id=? AND kind=? AND (request_key=? OR (version_id IS ? AND parameters=?)) ORDER BY created DESC LIMIT 1", (page_id, kind, key_hash, version_id, encoded(parameters))).fetchone()
            if prior and not force:
                if prior["status"] != "succeeded":
                    db.execute("UPDATE document_stages SET status='queued',error=NULL,phase='等待处理' WHERE id=?", (prior["id"],))
                return prior["id"]
            if force:
                key_hash += "-" + uid()
            key = uid()
            db.execute("""INSERT INTO document_stages(id,page_id,version_id,kind,request_key,parameters,created)
                VALUES(?,?,?,?,?,?,?)""", (key, page_id, version_id, kind, key_hash, encoded(parameters), now()))
            return key

    def recover_document_stages(self):
        with self.transaction() as db:
            db.execute("UPDATE document_stages SET status='interrupted',phase='等待继续',error='上次退出时阶段未完成' WHERE status='running'")
            db.execute("UPDATE document_stages SET status='paused',phase='等待继续' WHERE status='queued'")
            db.execute("UPDATE document_stages SET status='paused',phase='等待继续' WHERE status='waiting_gpu'")

    def claim_document_stage(self):
        from ocr_workbench.store import now
        with self.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM document_stages WHERE status='queued' ORDER BY created,id LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE document_stages SET status='running',attempt=attempt+1,started=?,phase='处理页面',error=NULL WHERE id=?", (now(), row["id"]))
            return dict(row)

    def finish_document_stage(self, key, output=None, error=None, *, waiting_unlock=False):
        from ocr_workbench.store import now, encoded
        with self.transaction() as db:
            return db.execute("""UPDATE document_stages SET status=?,phase=?,output=?,error=?,finished=?
                WHERE id=? AND status='running'""",
                ("waiting_unlock" if waiting_unlock else "failed" if error else "succeeded",
                 "等待解锁" if waiting_unlock else "处理失败" if error else "完成",
                 encoded(output) if output is not None else None, error, now(), key)).rowcount == 1
