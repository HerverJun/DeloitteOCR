"""Real filesystem and negative tests for the read-only development planner."""
from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/platform_plan.py"
SPEC = importlib.util.spec_from_file_location("platform_plan", SCRIPT)
planner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(planner)


class PlatformPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repository"
        self.repo.mkdir()
        self.run = self.root / "run"
        self.run.mkdir()
        self.plan = {
            "schema_version": 1, "plan_id": "test", "revision": 1,
            "repositories": {"platform": {"default_root": str(self.repo)}},
            "resources": ["source", "gpu"],
            "phases": [{"id": "P0"}],
            "execution": {"maximum_active_tasks": 1},
            "gates": [{"id": "engineering", "tasks": ["A", "C"]},
                      {"id": "device", "tasks": ["B"], "requires_gates": ["engineering"]}],
            "tasks": [self.task("A", [], ["source"], "logic"),
                      self.task("B", ["A"], ["gpu"], "gpu"),
                      self.task("C", ["A"], ["source"], "logic")],
        }
        self.checks = {"schema_version": 1, "plan_id": "test", "checks": [
            {"id": "logic", "producer_task": None, "evidence_kind": "native_cpu",
             "timeout_seconds": 10, "kind": "review"},
            {"id": "gpu", "producer_task": None, "evidence_kind": "real_gpu",
             "timeout_seconds": 10, "kind": "review"},
        ]}
        self.digest = "a" * 64
        self.state = planner.new_state(self.plan, self.digest, "test-run")

    def task(self, key, deps, resources, check):
        return {"id": key, "phase": "P0", "title": key, "priority": ord(key),
                "depends_on": deps, "resources": resources, "required_checks": [check],
                "write_scopes": {"platform": ["source/"]},
                "outputs": [{"repo": "platform", "path": f"source/{key}.txt"}],
                "steps": ["Implement"], "acceptance": ["Check behavior"], "max_attempts": 3}

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")

    def write_json(self, path, value):
        self.write(path, json.dumps(value, ensure_ascii=False, indent=2))

    def entry(self, repo, name):
        root = self.run if repo == "run" else self.repo
        return {"repo": repo, "path": name, "sha256": planner.file_hash(root / name)}

    def complete(self, key):
        task = next(t for t in self.plan["tasks"] if t["id"] == key)
        source = f"source/{key}-input.txt"
        output = f"source/{key}.txt"
        self.write(self.repo / source, f"input-{key}")
        self.write(self.repo / output, f"output-{key}")
        record = self.state["tasks"][key]
        record.update(status="done", attempt=1, source_inputs=[self.entry("platform", source)],
                      outputs=[self.entry("platform", output)])
        binding = planner.binding_digest(self.digest, key, record)
        for check_id in task["required_checks"]:
            log_path = f"attempts/{key}/001/{check_id}.log"
            self.write(self.run / log_path, "fixture harness: assertion passed\n")
            kind = next(c["evidence_kind"] for c in self.checks["checks"] if c["id"] == check_id)
            receipt = {"task_id": key, "check_id": check_id, "status": "passed", "exit_code": 0,
                       "evidence_kind": kind, "binding_digest": binding,
                       "log": {"path": log_path, "sha256": planner.file_hash(self.run / log_path)},
                       "test_counts": {"passed": 1, "failed": 0, "skipped": 0}}
            path = f"attempts/{key}/001/{check_id}.json"
            self.write_json(self.run / path, receipt)
            record["check_results"].append({"check_id": check_id, "path": path,
                                             "sha256": planner.file_hash(self.run / path)})

    def alter_receipt(self, key, **changes):
        item = self.state["tasks"][key]["check_results"][0]
        path = self.run / item["path"]
        receipt = planner.read_json(path)
        receipt.update(changes)
        self.write_json(path, receipt)
        item["sha256"] = planner.file_hash(path)

    def inspect(self):
        return planner.inspect_state(self.plan, self.checks, self.digest, self.state, self.run)

    def select(self, available=()):
        return planner.select_next(self.plan, self.checks, self.digest, self.state, self.run, available)

    def test_repository_plan_is_acyclic_and_has_resolved_checks(self):
        plan, checks, digest = planner.load_bundle(planner.DEFAULT_PLAN)
        self.assertEqual(len(plan["tasks"]), 47)
        self.assertEqual(len(checks["checks"]), 47)
        self.assertEqual(len(digest), 64)
        first = planner.select_next(plan, checks, digest, planner.new_state(plan, digest, "preview"), self.run)
        self.assertEqual([t["id"] for t in first["ready"]], ["T001"])

    def test_no_gpu_does_not_block_independent_cpu_work(self):
        self.complete("A")
        result = self.select()
        self.assertEqual([t["id"] for t in result["ready"]], ["C"])
        missing = next(t for t in result["blocked"] if t["id"] == "B")
        self.assertEqual(missing["missing_resources"], ["gpu"])

    def test_real_gpu_and_business_independent_gates_are_not_merged(self):
        self.complete("A")
        self.complete("C")
        result = self.select()
        self.assertTrue(result["gates"]["engineering"]["passed"])
        self.assertFalse(result["gates"]["device"]["passed"])
        self.assertFalse(result["all_done"])

    def test_complete_with_valid_evidence_unlocks_dependencies(self):
        self.complete("A")
        self.assertEqual(self.inspect()["verified"], {"A"})
        self.assertEqual([t["id"] for t in self.select(["gpu"])["ready"]], ["B", "C"])

    def test_done_without_artifacts_does_not_unlock_work(self):
        self.state["tasks"]["A"].update(status="done", attempt=1)
        result = self.select()
        self.assertFalse(result["ready"])
        self.assertEqual(result["reverify"][0]["id"], "A")

    def test_changed_source_invalidates_evidence_and_children(self):
        self.complete("A")
        self.complete("C")
        self.write(self.repo / "source/A-input.txt", "changed")
        issues = self.inspect()["issues"]
        self.assertIn("Stale/hash mismatch", issues["A"])
        self.assertIn("dependencies", issues["C"])

    def test_updated_source_hash_still_requires_new_check_binding(self):
        self.complete("A")
        self.write(self.repo / "source/A-input.txt", "changed")
        self.state["tasks"]["A"]["source_inputs"] = [self.entry("platform", "source/A-input.txt")]
        self.assertIn("stale check binding", self.inspect()["issues"]["A"])

    def test_modified_output_is_not_accepted(self):
        self.complete("A")
        self.write(self.repo / "source/A.txt", "corrupt")
        self.assertIn("Stale/hash mismatch", self.inspect()["issues"]["A"])

    def test_failed_receipt_is_not_a_pass(self):
        self.complete("A")
        self.alter_receipt("A", status="failed", exit_code=1)
        self.assertIn("did not pass", self.inspect()["issues"]["A"])

    def test_boolean_zero_cannot_impersonate_exit_code(self):
        self.complete("A")
        self.alter_receipt("A", exit_code=False)
        self.assertIn("did not pass", self.inspect()["issues"]["A"])

    def test_skipped_only_suite_is_not_a_pass(self):
        self.complete("A")
        self.alter_receipt("A", test_counts={"passed": 0, "failed": 0, "skipped": 5})
        self.assertIn("no passed assertions", self.inspect()["issues"]["A"])

    def test_failed_assertion_cannot_hide_behind_zero_exit(self):
        self.complete("A")
        self.alter_receipt("A", test_counts={"passed": 5, "failed": 1, "skipped": 0})
        self.assertIn("failed assertions", self.inspect()["issues"]["A"])

    def test_test_runner_cannot_omit_assertion_counts(self):
        self.checks["checks"][0].update(kind="command", command={
            "runtime": "platform_python", "cwd": "platform",
            "args": ["-m", "pytest", "tests/unit", "-q"]})
        self.complete("A")
        self.alter_receipt("A", test_counts=None)
        self.assertIn("missing test_counts", self.inspect()["issues"]["A"])

    def test_test_count_policy_distinguishes_builds_and_test_runners(self):
        for args in (["-m", "unittest", "discover"], ["run", "test"],
                     ["tests/e2e/navigation.mjs"]):
            with self.subTest(args=args):
                self.assertTrue(planner.requires_test_counts({"command": {"args": args}}))
        self.assertFalse(planner.requires_test_counts({"command": {"args": ["run", "build"]}}))
        self.assertTrue(planner.requires_test_counts({"requires_test_counts": True}))

    def test_simulated_evidence_cannot_satisfy_gpu_task(self):
        self.complete("A")
        self.complete("B")
        self.alter_receipt("B", evidence_kind="simulated_process")
        self.assertIn("evidence kind mismatch", self.inspect()["issues"]["B"])

    def test_missing_log_is_not_accepted(self):
        self.complete("A")
        (self.run / "attempts/A/001/logic.log").unlink()
        self.assertIn("Missing file", self.inspect()["issues"]["A"])

    def test_empty_log_is_not_accepted_even_with_updated_hash(self):
        self.complete("A")
        path = self.run / "attempts/A/001/logic.log"
        self.write(path, "")
        self.alter_receipt("A", log={"path": "attempts/A/001/logic.log", "sha256": planner.file_hash(path)})
        self.assertIn("empty raw log", self.inspect()["issues"]["A"])

    def test_receipt_cannot_read_outside_run_directory(self):
        self.complete("A")
        self.state["tasks"]["A"]["check_results"][0]["path"] = "../outside.json"
        self.assertIn("Path traversal", self.inspect()["issues"]["A"])

    def test_invalid_paths_are_rejected(self):
        for path in ("../outside", "C:/secret", "C:secret", "/outside", "a/../../b", "x:stream", ""):
            with self.subTest(path=path), self.assertRaises(planner.PlanError):
                planner.relative_path(path)

    def test_cycle_and_unknown_dependencies_are_rejected(self):
        for dependency in ("B", "missing"):
            with self.subTest(dependency=dependency):
                plan = deepcopy(self.plan)
                plan["tasks"][0]["depends_on"] = [dependency]
                with self.assertRaises(planner.PlanError):
                    planner.validate_plan(plan, self.checks)

    def test_future_check_producer_must_be_declared_dependency(self):
        checks = deepcopy(self.checks)
        checks["checks"][0]["producer_task"] = "B"
        with self.assertRaisesRegex(planner.PlanError, "unmet dependency"):
            planner.validate_plan(self.plan, checks)

    def test_duplicate_ids_are_rejected(self):
        self.plan["tasks"].append(deepcopy(self.plan["tasks"][0]))
        with self.assertRaisesRegex(planner.PlanError, "Duplicate task ID"):
            planner.validate_plan(self.plan, self.checks)

    def test_output_must_be_inside_declared_write_scope(self):
        for path in ("outside/A.txt", "source-adjacent/A.txt"):
            with self.subTest(path=path):
                plan = deepcopy(self.plan)
                plan["tasks"][0]["outputs"][0]["path"] = path
                with self.assertRaisesRegex(planner.PlanError, "outside declared write scope"):
                    planner.validate_plan(plan, self.checks)

    def test_plan_revision_mismatch_is_not_silently_migrated(self):
        self.state["plan_digest"] = "b" * 64
        with self.assertRaisesRegex(planner.PlanError, "different plan revision"):
            self.inspect()

    def test_running_task_is_resumed_without_starting_new_task(self):
        self.complete("A")
        self.state["tasks"]["C"].update(status="running", attempt=1)
        result = self.select(["gpu"])
        self.assertEqual(result["resume"], ["C"])
        self.assertEqual(result["ready"], [])

    def test_attempt_limit_and_failed_status_do_not_auto_retry(self):
        self.complete("A")
        self.state["tasks"]["B"].update(attempt=3)
        self.state["tasks"]["C"].update(status="failed", attempt=1)
        self.assertEqual(self.select(["gpu"])["ready"], [])

    def test_invalid_json_duplicate_keys_and_nan_are_rejected(self):
        path = self.root / "bad.json"
        for text in ('{"key": 1, "key": 2}', '{"key": NaN}'):
            self.write(path, text)
            with self.assertRaises(planner.PlanError):
                planner.read_json(path)

    def test_init_state_refuses_overwrite_and_starts_no_tasks(self):
        plan_path, checks_path = self.root / "plan.json", self.root / "checks.json"
        self.write_json(plan_path, self.plan)
        self.write_json(checks_path, self.checks)
        target = self.root / "fresh"
        command = ["--plan", str(plan_path), "--checks", str(checks_path), "init-state",
                   "--run-dir", str(target), "--run-id", "test-run"]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(planner.main(command), 0)
            original = (target / "state.json").read_bytes()
            self.assertEqual(planner.main(command), 2)
        self.assertEqual(original, (target / "state.json").read_bytes())
        state = planner.read_json(target / "state.json")
        self.assertTrue(all(t["status"] == "pending" for t in state["tasks"].values()))


if __name__ == "__main__":
    unittest.main()
