"""Visual review routes with separate local GPU and external network queues."""
import shutil

from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from ocr_workbench.editing import present_result


def register_multimodal_routes(app, store, bundle, queue, maintenance, require_recognition):
    from ocr_workbench.multimodal_store import enqueue_review, view_review, decide_review, lookup_existing_review
    from ocr_workbench.multimodal_runtime import load_config, review_readiness

    external = app.state.external_connection
    external_queue = app.state.external_queue

    @app.get("/api/multimodal/external")
    def external_view():
        return external.view()

    @app.post("/api/multimodal/external/models")
    def external_models(body: dict):
        return external.models(body)

    @app.put("/api/multimodal/external")
    def external_save(body: dict):
        return external.save(body)

    @app.delete("/api/multimodal/external")
    def external_clear():
        return external.clear()

    def local_models():
        try:
            config = load_config(bundle)
        except (ValueError, OSError, KeyError) as error:
            return {"models": [], "default_model": None, "reason": str(error),
                    "policy": {"automatic_acceptance": False, "offline": True}}
        ready = review_readiness(bundle, config)
        entries = []
        # Profile selection is independent of the four original OCR engines.
        for profile in ready.get("profiles", []):
            identifier = profile if isinstance(profile, str) else profile.get("id", profile.get("profile_id"))
            if not identifier:
                continue
            try:
                item = review_readiness(bundle, load_config(bundle, identifier))
            except (ValueError, OSError, KeyError, TypeError) as error:
                item = {"ready": False, "label": profile.get("label", identifier) if isinstance(profile, dict) else identifier,
                        "reason": str(error)}
            entries.append({"id": identifier, "label": item.get("label", identifier),
                            "available": bool(item["ready"]) and not app.state.review_only,
                            "reason": "仅校对模式不启动视觉模型" if app.state.review_only else item.get("reason", ""),
                            "identity": item.get("identity")})
        if not entries:
            entries = [{"id": config["profile_id"], "label": ready.get("label", config["profile_id"]),
                        "available": bool(ready["ready"]) and not app.state.review_only,
                        "reason": "仅校对模式不启动视觉模型" if app.state.review_only else ready.get("reason", "")}]
        return {"models": entries, "default_model": config["profile_id"],
                "policy": {"automatic_acceptance": False, "offline": True,
                           "prompt_version": config.get("prompt_version")}}

    @app.get("/api/multimodal/models")
    def models():
        catalog = local_models()
        for item in catalog['models']:
            item['backend'] = 'local'
        remote = external.view()
        if remote['configured']:
            queue_ready = not app.state.start_queue or external_queue.status()['healthy']
            catalog['models'].append({'id': remote['model_id'], 'label': '外部 API · ' + remote['model'],
                'available': remote['available'] and queue_ready, 'reason': remote['reason'] if queue_ready else '外部审校队列异常，请恢复队列', 'backend': 'external',
                'base_url': remote['base_url'], 'protocol': remote['protocol'], 'model': remote['model']})
            catalog['policy']['external_queue_healthy'] = queue_ready
        catalog['policy']['offline'] = not remote['configured']
        return catalog

    @app.get("/api/results/{key}/multimodal")
    def view(key: str):
        return view_review(store, key)

    @app.post("/api/results/{key}/multimodal")
    def enqueue(key: str, body: dict):
        return app.state.application_services.submit_visual_review(key, body)

    @app.post("/api/results/{key}/multimodal/{proposal_id}/decision")
    def decide(key: str, proposal_id: str, body: dict):
        return present_result(decide_review(store, key, proposal_id, body))

    @app.post("/api/results/{key}/multimodal/tasks/{task_id}/{action}")
    def task_action(key: str, task_id: str, action: str, body: dict):
        if action not in {"cancel", "retry", "resume"}:
            raise ValueError("未知审校任务操作")
        rows = store.rows("""SELECT t.project_id,mr.backend FROM multimodal_requests mr
            JOIN tasks t ON t.id=mr.task_id WHERE mr.task_id=? AND mr.result_id=?""", (task_id, key))
        if not rows:
            raise ValueError("审校任务不属于当前结果")
        target_queue = external_queue if rows[0]['backend'] == 'external' else queue
        if action != 'cancel' and rows[0]['backend'] != 'external':
            require_recognition()
        if action != "cancel":
            # Retry preserves its original snapshot. A new revision requires a new review.
            from ocr_workbench.multimodal_store import prepare_review
            prepare_review(store, task_id, require_running=False)
        affected = target_queue.action(rows[0]["project_id"], action, [task_id])
        return {"task_ids": affected}

    @app.get("/api/results/{key}/multimodal/report")
    def report(key: str, format: str = "xlsx"):
        from ocr_workbench.multimodal_export import build_review_report
        with maintenance.guard:
            target = build_review_report(store, key, format)
        return FileResponse(target, filename=target.name,
                            background=BackgroundTask(shutil.rmtree, target.parent))
