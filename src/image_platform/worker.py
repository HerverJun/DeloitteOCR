from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import threading
import time
import traceback

import numpy as np

from .datasets import register_asset
from .images import SUPPORTED, preprocess
from .runtime import WORKER_SESSIONS, decide, validate_package
from .settings import Settings
from .store import Store, dump, now, uid


class Cancelled(Exception):
    pass


class Paused(Exception):
    pass


def check_control(store, job):
    current = store.job(job["id"])
    if current["lease_token"] != job["lease_token"] or (current["lease_until"] or 0) <= time.time():
        raise RuntimeError("任务租约已经失效")
    if current["state"] == "cancelling":
        raise Cancelled()
    if current["state"] == "pausing":
        raise Paused()


def import_directory(store, job, control, log):
    project = store.project(job["project_id"])
    root = Path(job["params"]["path"]).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("导入路径不是目录")
    label_lookup = {key: label["label_id"] for label in project["labels"] for key in (label["label_id"], label["display_name"])}
    source_id = job["params"].get("source_id") or root.name
    scanned = imported = duplicates = failed = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not (Path(current) / name).is_symlink())
        for name in sorted(files):
            if Path(name).suffix.lower() not in SUPPORTED:
                continue
            control()
            path = Path(current) / name
            scanned += 1
            try:
                resolved = path.resolve(strict=True)
                if path.is_symlink() or not resolved.is_relative_to(root):
                    raise ValueError("文件路径越出登记目录")
                relative = resolved.relative_to(root)
                label_id = label_lookup.get(relative.parts[0]) if job["params"].get("infer_labels", True) and len(relative.parts) > 1 else None
                _, created = register_asset(store, project["id"], resolved, label_id, source_id, job=job)
                imported += int(created)
                duplicates += int(not created)
            except (OSError, ValueError) as exc:
                failed += 1
                log(f"Import failed: {path.name}: {exc}")
                with store.connect(write=True) as con:
                    store.assert_owner(con, job["id"], job["lease_token"])
                    con.execute("INSERT INTO job_items(id,job_id,path,sha256,state,error) VALUES(?,?,?,'','failed',?)", (uid("item"), job["id"], str(path), str(exc)))
            if scanned % 10 == 0:
                store.progress(job["id"], job["lease_token"], "扫描和校验图片", scanned, scanned)
    with store.connect(write=True) as con:
        store.assert_owner(con, job["id"], job["lease_token"])
        con.execute("UPDATE jobs SET total=?,done=?,failed=?,stage=? WHERE id=?", (scanned, scanned - failed, failed, "导入完成", job["id"]))
    return {"scanned": scanned, "imported": imported, "duplicates": duplicates, "failed": failed}


def commit_results(store, job, results):
    with store.connect(write=True) as con:
        store.assert_owner(con, job["id"], job["lease_token"])
        done = failed = 0
        for item, prediction, error in results:
            changed = con.execute("UPDATE job_items SET state=?,prediction=?,error=?,attempts=attempts+1 WHERE id=? AND job_id=? AND state='pending'",
                                  ("failed" if error else "succeeded", dump(prediction) if prediction else None, error, item["id"], job["id"])).rowcount
            if changed:
                done += int(not error)
                failed += int(bool(error))
        if done or failed:
            con.execute("UPDATE jobs SET done=done+?,failed=failed+?,updated_at=? WHERE id=?", (done, failed, now(), job["id"]))


def run_inference(store, job, control, log):
    execution_started = time.perf_counter()
    params = job["params"]
    row = store.one("SELECT * FROM models WHERE id=? AND project_id=?", (params["model_id"], job["project_id"]))
    if not row:
        raise ValueError("模型不存在或不属于此项目")
    metadata = validate_package(row["package_path"])
    if params["model_sha256"] != metadata["provenance"]["model_onnx_sha256"]:
        raise ValueError("任务绑定的模型哈希已变化")
    timings = {"package_validation_seconds": time.perf_counter() - execution_started,
               "preprocess_wait_seconds": 0.0, "preprocess_worker_seconds": 0.0,
               "inference_seconds": 0.0, "commit_seconds": 0.0}
    before = time.perf_counter()
    session, cache_hit = WORKER_SESSIONS.get(Path(row["package_path"]) / "model.onnx", params["model_sha256"], params["device"] == "cuda")
    timings["session_seconds"] = time.perf_counter() - before
    batch_size = params["batch_size"]
    started = time.perf_counter()
    processed = 0
    height, width = metadata["input"]["height"], metadata["input"]["width"]
    store.progress(job["id"], job["lease_token"], "批量分类")

    def prepare(item):
        before = time.perf_counter()
        try:
            tensor, error = preprocess(item["path"], item["sha256"], height, width, metadata["preprocessing"]), None
        except (OSError, ValueError) as exc:
            tensor, error = None, str(exc)
        return tensor, error, time.perf_counter() - before

    def pending(after=""):
        return store.rows("SELECT * FROM job_items WHERE job_id=? AND state='pending' AND id>? ORDER BY id LIMIT ?",
                          (job["id"], after, params["batch_size"]))

    # At most the current and one next batch are decoded. The coordinator is
    # separate from decode workers, avoiding a nested-executor deadlock.
    with ThreadPoolExecutor(max_workers=4) as decoders, ThreadPoolExecutor(max_workers=1) as prefetch:
        def prepare_batch(batch):
            return list(decoders.map(prepare, batch))

        batch = pending()
        future = prefetch.submit(prepare_batch, batch) if batch else None
        while batch:
            control()
            before = time.perf_counter()
            prepared = future.result()
            timings["preprocess_wait_seconds"] += time.perf_counter() - before
            timings["preprocess_worker_seconds"] += sum(elapsed for _, _, elapsed in prepared)
            control()
            next_batch = pending(batch[-1]["id"])
            next_future = prefetch.submit(prepare_batch, next_batch) if next_batch else None
            good = [(item, tensor) for item, (tensor, error, _) in zip(batch, prepared, strict=True) if not error]
            results = [(item, None, error) for item, (_, error, _) in zip(batch, prepared, strict=True) if error]
            offset = 0
            while offset < len(good):
                control()
                chunk = good[offset:offset + batch_size]
                before = time.perf_counter()
                try:
                    logits = session.run(["logits"], {"images": np.stack([tensor for _, tensor in chunk])})[0]
                    decisions = decide(logits, metadata, params["auto_accept"])
                    for (item, _), decision in zip(chunk, decisions, strict=True):
                        decision.update(model_version_id=row["id"], policy_version=metadata["decision_policy"]["version"], processed_at=now())
                        results.append((item, decision, None))
                except Exception as exc:
                    message = str(exc)
                    if any(term in message.lower() for term in ("out of memory", "cuda failure 2", "allocate memory")) and batch_size > 1:
                        batch_size = max(1, batch_size // 2)
                        log(f"显存不足，batch 降至 {batch_size} 后重试")
                        continue
                    if batch_size > 1:
                        # Isolate a problematic sample before declaring a row failed.
                        batch_size = 1
                        log(f"Batch inference failed; retrying individually: {message[:300]}")
                        continue
                    results.extend((item, None, message[:2000]) for item, _ in chunk)
                finally:
                    timings["inference_seconds"] += time.perf_counter() - before
                offset += len(chunk)
            control()
            before = time.perf_counter()
            commit_results(store, job, results)
            timings["commit_seconds"] += time.perf_counter() - before
            processed += len(results)
            batch, future = next_batch, next_future
    elapsed = time.perf_counter() - started
    result = {"processed_this_run": processed, "elapsed_seconds": elapsed,
              "images_per_second_this_run": processed / elapsed if elapsed else 0, "batch_size_final": batch_size,
              "providers": session.get_providers(), "auto_accept": params["auto_accept"],
              "execution_seconds": time.perf_counter() - execution_started, "stage_timings": timings,
              "session_cache_hit": cache_hit, "prefetch_batches": 1, "decode_workers": 4}
    log(f"Inference finished: {processed} items in {elapsed:.2f}s")
    return result


def run_job(store, job):
    log_path = store.settings.data_dir / "logs" / f"{job['id']}.log"

    def log(message):
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(f"{now()} {message}\n")

    stop = threading.Event()

    def heartbeat():
        while not stop.wait(5):
            try:
                if not store.heartbeat(job["id"], job["lease_token"]):
                    break
            except Exception as exc:
                log(f"Heartbeat error: {exc}")

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    def control():
        return check_control(store, job)
    try:
        log(f"Started {job['kind']}")
        control()
        if job["kind"] == "import":
            result = import_directory(store, job, control, log)
        elif job["kind"] == "train":
            WORKER_SESSIONS.clear()
            from .training import train
            result = train(store, job, control, log)
        elif job["kind"] == "inference":
            result = run_inference(store, job, control, log)
        else:
            raise ValueError("未知任务类型")
        current = store.job(job["id"])
        store.finish(job["id"], job["lease_token"], "completed_with_errors" if current["failed"] else "succeeded", result)
    except Paused:
        store.finish(job["id"], job["lease_token"], "paused")
        log("Paused at a committed batch boundary")
    except Cancelled:
        store.finish(job["id"], job["lease_token"], "cancelled")
        log("Cancelled")
    except Exception as exc:
        log(traceback.format_exc())
        try:
            store.finish(job["id"], job["lease_token"], "failed", error=str(exc)[:3000])
        except Exception:
            log("Lease lost; recovery will determine the final job state")
    finally:
        stop.set()
        thread.join(timeout=6)


def serve_worker(settings=None, once=False):
    store = Store(settings or Settings.from_env())
    owner = f"worker-{os.getpid()}"
    while True:
        job = store.claim(owner)
        if job:
            run_job(store, job)
        elif once:
            return
        else:
            time.sleep(1)
        if once:
            return
