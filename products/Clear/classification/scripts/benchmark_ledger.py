"""Exercise a 150k-item job ledger and recovery without claiming model throughput."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time

from image_platform.settings import Settings
from image_platform.store import Store, dump, now
from image_platform.worker import commit_results


def main():
    with tempfile.TemporaryDirectory(prefix="ledger-", dir=Path("reports")) as directory:
        store = Store(Settings(Path(directory)))
        with store.connect(write=True) as con:
            con.execute("INSERT INTO projects(id,name,labels,created_at) VALUES('benchmark','账本测试',?,?)", (dump([]), now()))
        count = 150_000
        started = time.perf_counter()
        job_id = store.submit("benchmark", "inference", {"synthetic_ledger_only": True},
                              ({"id": f"input_{i}", "path": f"synthetic/{i}.png", "sha256": "a" * 64} for i in range(count)))
        creation_seconds = time.perf_counter() - started
        job = store.claim("initial-worker")
        first_batch = store.rows("SELECT * FROM job_items WHERE job_id=? AND state='pending' ORDER BY id LIMIT 64", (job_id,))
        commit_results(store, job, [(item, {"label_id": "synthetic", "status": "needs_review", "score": 0.5}, None) for item in first_batch])
        with store.connect(write=True) as con:
            con.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (job_id,))
            con.execute("UPDATE leases SET expires=0")
        recovered = store.claim("recovered-worker")
        assert recovered and recovered["id"] == job_id
        assert recovered["done"] == 64
        pending = store.one("SELECT COUNT(*) count FROM job_items WHERE job_id=? AND state='pending'", (job_id,))["count"]
        assert pending == count - 64
        started = time.perf_counter()
        with store.connect() as con:
            for _ in range(100):
                con.execute("SELECT * FROM job_items WHERE job_id=? AND state='pending' ORDER BY id LIMIT 64", (job_id,)).fetchall()
            plan = [dict(row) for row in con.execute("EXPLAIN QUERY PLAN SELECT * FROM job_items WHERE job_id=? AND state='pending' ORDER BY id LIMIT 64", (job_id,))]
        query_seconds = time.perf_counter() - started
        assert not any("TEMP B-TREE" in row["detail"] for row in plan)
        assert store.one("SELECT COUNT(DISTINCT asset_id) count FROM job_items WHERE job_id=?", (job_id,))["count"] == count
        report = {"checked_at": datetime.now(timezone.utc).isoformat(), "status": "passed", "input_count": count,
                  "committed_before_crash": 64, "pending_after_recovery": pending, "creation_seconds": creation_seconds,
                  "query_64_items_mean_ms": query_seconds / 100 * 1000, "query_plan": plan,
                  "scope": "Database input identity, committed-result recovery and pending-batch query only; no image decoding or model inference."}
        Path("reports/ledger-150k.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
