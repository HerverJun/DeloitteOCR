"""CPU scheduling contracts with the real manager, queue and SQLite store.

The existing controlled adapter emits fixed synthetic tables. These tests prove
group boundaries, durable identities and cache behavior, not OCR quality or
measured time to a first real model result.
"""
from collections import Counter
import json
from pathlib import Path
import shutil

import pytest

import test_screen_queue as native_fixture
from ocr_workbench.imaging import add_image
from ocr_workbench.screen_batches import ScreenBatches
from ocr_workbench.store import Store
from ocr_workbench.task_queue import TaskQueue


@pytest.fixture
def harness(tmp_path, monkeypatch):
    assert tmp_path.resolve().drive.upper() == "F:"
    monkeypatch.setattr(native_fixture, "TEMP_ROOT", tmp_path / "controlled-queue")
    fixture = native_fixture.ScreenQueueTests()
    fixture.setUp()
    fixture.prepared_versions = []
    fixture.failed_preparations = set()

    def tracked_prepare(source, folder, **kwargs):
        version = Path(folder).parent.name
        fixture.prepared_versions.append(version)
        if version in fixture.failed_preparations:
            raise RuntimeError("controlled preparation failure")
        return native_fixture.prepare(source, folder, **kwargs)

    fixture.manager.prepare_override = tracked_prepare
    try:
        yield fixture
    finally:
        fixture.tearDown()


def submit(fixture, versions, *, grouped=True):
    policy = {"group_size": 2, "budget_seconds": 600, "require_human_review": True,
              "request_timeout_seconds": 120, "batch_budget_seconds": 36000}
    if grouped:
        policy["context_schedule"] = "grouped"
    return fixture.submit(versions, **policy)


def contexts(fixture):
    return fixture.store.rows("""SELECT c.*,t.status,t.kind FROM screen_contexts c
        JOIN tasks t ON t.id=c.task_id ORDER BY t.created,t.id""")


def recognition(fixture):
    return fixture.store.rows("""SELECT s.*,t.status,t.kind FROM screen_tasks s
        JOIN tasks t ON t.id=s.task_id ORDER BY t.created,t.id""")


def compute_identities(fixture, versions):
    return {row["task_id"] for row in contexts(fixture) + recognition(fixture)
            if row["input_version_id"] in versions}


def run_queue(fixture, count):
    for _ in range(count):
        assert fixture.queue.step() is True


def test_grouped_context_prepares_only_the_first_group_and_waits_for_every_resident(harness):
    # A partially completed resident group must block all next-group contexts.
    fixture = harness
    versions = fixture.images(5)
    batch = submit(fixture, versions)
    fixture.manager.step()
    assert {row["input_version_id"] for row in contexts(fixture)} == set(versions[:2])
    assert recognition(fixture) == []
    assert fixture.prepared_versions == []

    run_queue(fixture, 1)
    fixture.manager.step()
    assert len(contexts(fixture)) == 2
    assert recognition(fixture) == []
    assert fixture.prepared_versions == []

    run_queue(fixture, 1)
    fixture.manager.step()
    assert Counter(fixture.prepared_versions) == Counter(versions[:2])
    assert {row["input_version_id"] for row in recognition(fixture)} == set(versions[:2])
    run_queue(fixture, 1)
    for _ in range(3):
        fixture.manager.step()
    assert len(contexts(fixture)) == 2
    assert Counter(fixture.prepared_versions) == Counter(versions[:2])
    assert fixture.manager.get(fixture.project, batch)["counts"]["needs_review"] == 1

    run_queue(fixture, 1)
    fixture.manager.step()
    assert fixture.manager.get(fixture.project, batch)["counts"]["needs_review"] == 2
    assert len(contexts(fixture)) == 2
    fixture.manager.step()
    assert {row["input_version_id"] for row in contexts(fixture)} == set(versions[:4])
    assert Counter(fixture.prepared_versions) == Counter(versions[:2])

    fixture.drain()
    assert fixture.manager.get(fixture.project, batch)["counts"]["needs_review"] == 5
    assert Counter(fixture.prepared_versions) == Counter(versions)
    engines = [event[1] for event in fixture.events if event[0] == "load"]
    assert engines == ["geometry", "glm", "geometry", "glm", "geometry", "glm"]


@pytest.mark.parametrize("failure_stage", ["context", "preparation", "ocr"])
def test_failed_first_group_retains_its_failure_and_later_groups_continue(harness, failure_stage):
    fixture = harness
    versions = fixture.images(4)
    batch = submit(fixture, versions)
    fixture.manager.step()
    if failure_stage == "context":
        fixture.failure = "geometry"
    if failure_stage == "preparation":
        fixture.failed_preparations.update(versions[:2])
    run_queue(fixture, 2)
    fixture.manager.step()
    assert len(contexts(fixture)) == 2
    if failure_stage == "ocr":
        fixture.failure = "glm"
    fixture.drain()

    details = [fixture.manager.detail(fixture.project, batch, version) for version in versions]
    assert [item["status"] for item in details[2:]] == ["needs_review", "needs_review"]
    assert all(item["result"] and item["human_confirmed"] is False for item in details[2:])
    assert Counter(fixture.prepared_versions) == Counter(versions)
    assert len(contexts(fixture)) == 4
    assert fixture.manager.get(fixture.project, batch)["counts"]["pending"] == 0
    if failure_stage == "context":
        assert any(item["status"] == "needs_review" and
                   "context_failed" in {issue["code"] for issue in item["issues"]}
                   for item in details[:2])
        assert sum(row["status"] == "failed" for row in contexts(fixture)) == 1
    elif failure_stage == "preparation":
        assert all(item["status"] == "failed" and item["result"] is None for item in details[:2])
        assert len(recognition(fixture)) == 2
    else:
        assert any(item["status"] == "failed" and item["result"] is None for item in details[:2])
        assert sum(row["status"] == "failed" for row in recognition(fixture)) == 1


def test_exact_duplicate_in_later_group_reuses_both_stages_with_its_own_identity(harness):
    fixture = harness
    originals = fixture.images(3)
    source = fixture.store.file(fixture.store.one("versions", originals[0])["path"])
    copied = fixture.root / "duplicate-input.png"
    shutil.copyfile(source, copied)
    duplicate = add_image(fixture.store, fixture.project, "相同原像素.png", copied)["active_version"]
    versions = [originals[0], originals[1], duplicate, originals[2]]
    batch = submit(fixture, versions)
    fixture.drain()

    state = fixture.manager.get(fixture.project, batch)
    assert state["counts"]["needs_review"] == 4, {
        "counts": state["counts"],
        "items": [{key: item.get(key) for key in ("input_version_id", "status", "stage", "error")}
                  for item in state["items"]],
    }
    assert Counter(call["engine"] for call in fixture.calls) == {"geometry": 3, "glm": 3}
    cached_context = next(row for row in contexts(fixture) if row["input_version_id"] == duplicate)
    assert cached_context["kind"] == "screen_context_cached"
    context_raw = json.loads(cached_context["raw"])
    assert context_raw["image_version"] == duplicate
    assert context_raw["cache_hit"]["source_image_version"] == originals[0]
    cached_ocr = next(row for row in recognition(fixture) if row["input_version_id"] == duplicate)
    assert cached_ocr["kind"] == "screen_cached"
    assert cached_ocr["status"] == "succeeded"
    results = [fixture.manager.detail(fixture.project, batch, version)["result"] for version in versions]
    assert len({result["id"] for result in results}) == 4
    assert [result["original"]["project_image_version"] for result in results] == versions
    assert results[2]["original"]["screen"]["sources"][0]["cache_hit"]["source_image_version"] == originals[0]
    assert Counter(fixture.prepared_versions) == Counter(versions)


@pytest.mark.parametrize("pause_stage", ["context", "ocr"])
def test_pause_cold_reopen_preserves_group_policy_and_never_repeats_completed_tasks(harness, pause_stage):
    fixture = harness
    versions = fixture.images(4)
    batch = submit(fixture, versions)
    fixture.manager.step()
    run_queue(fixture, 1)
    fixture.manager.step()
    if pause_stage == "ocr":
        run_queue(fixture, 1)
        fixture.manager.step()
        run_queue(fixture, 1)
        fixture.manager.step()
    original_ids = compute_identities(fixture, versions[:2])
    completed_ids = {row["task_id"] for row in contexts(fixture) + recognition(fixture)
                     if row["status"] == "succeeded"}
    fixture.manager.action(fixture.project, batch, {"action": "pause"})
    fixture.manager.step()
    assert len(contexts(fixture)) == 2
    fixture.manager.stop()
    fixture.queue.stop()
    fixture.queue.unload()

    factory = fixture.queue.factory
    prepare = fixture.manager.prepare_override
    fixture.store = Store(fixture.store.root)
    fixture.queue = TaskQueue(fixture.store, fixture.bundle, factory)
    fixture.manager = ScreenBatches(fixture.store, fixture.queue, fixture.bundle,
                                   prepare=prepare, select=native_fixture.select)
    assert fixture.manager.get(fixture.project, batch)["policy"]["context_schedule"] == "grouped"
    assert fixture.manager.get(fixture.project, batch)["status"] == "paused"
    fixture.manager.step()
    assert compute_identities(fixture, versions[:2]) == original_ids
    fixture.manager.action(fixture.project, batch, {"action": "resume"})
    fixture.manager.step()
    assert compute_identities(fixture, versions[:2]) == original_ids
    assert len(contexts(fixture)) == 2
    assert all(fixture.store.one("tasks", task_id)["status"] == "succeeded" for task_id in completed_ids)
    fixture.drain()
    assert fixture.manager.get(fixture.project, batch)["counts"]["needs_review"] == 4
    assert len(contexts(fixture)) == 4
    assert len(recognition(fixture)) == 4
    assert Counter(call["engine"] for call in fixture.calls) == {"geometry": 4, "glm": 4}
    assert Counter(fixture.prepared_versions) == Counter(versions)
    assert compute_identities(fixture, versions[:2]) == original_ids | {
        row["task_id"] for row in recognition(fixture) if row["input_version_id"] in versions[:2]}


def test_omitted_context_schedule_keeps_the_existing_whole_batch_barrier(harness):
    fixture = harness
    versions = fixture.images(5)
    batch = submit(fixture, versions, grouped=False)
    assert fixture.manager.get(fixture.project, batch)["policy"]["context_schedule"] == "whole_batch"
    fixture.manager.step()
    assert {row["input_version_id"] for row in contexts(fixture)} == set(versions)
    run_queue(fixture, 2)
    fixture.manager.step()
    assert fixture.prepared_versions == []
    assert recognition(fixture) == []
    run_queue(fixture, 3)
    fixture.manager.step()
    assert Counter(fixture.prepared_versions) == Counter(versions[:2])
    assert {row["input_version_id"] for row in recognition(fixture)} == set(versions[:2])
    fixture.drain()
    assert fixture.manager.get(fixture.project, batch)["counts"]["needs_review"] == 5
