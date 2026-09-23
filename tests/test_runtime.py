import numpy as np
import pytest

from image_platform.runtime import decide, probabilities


def test_insufficient_calibration_rejects_even_saturated_confidence():
    metadata = {"labels": [{"class_index": 1, "label_id": "b", "display_name": "B"}, {"class_index": 0, "label_id": "a", "display_name": "A"}],
                "decision_policy": {"thresholds_by_label_id": {"a": 1.0, "b": 0.8}}}
    logits = np.array([[1000.0, -1000.0], [-1000.0, 1000.0]], dtype=np.float32)
    rows = decide(logits, metadata, auto_accept=True)
    assert rows[0]["score"] == 1.0
    assert [(row["label_id"], row["status"]) for row in rows] == [("a", "needs_review"), ("b", "accepted")]
    assert all(row["status"] == "needs_review" for row in decide(logits, metadata))


def test_invalid_logits_are_rejected_before_decisions():
    with pytest.raises(ValueError):
        probabilities(np.array([[float("nan"), 0.0]]))
    with pytest.raises(ValueError):
        probabilities(np.array([1.0, 2.0]))


def test_session_cache_has_bounded_capacity_and_keys_content_and_device(tmp_path):
    from image_platform.runtime import SessionCache
    calls = []
    def factory(path, require_gpu):
        result = object()
        calls.append(result)
        return result
    cache = SessionCache(1)
    first, hit = cache.get(tmp_path / "model.onnx", "sha-one", factory=factory)
    assert not hit
    assert cache.get(tmp_path / "model.onnx", "sha-one", factory=factory) == (first, True)
    second, hit = cache.get(tmp_path / "model.onnx", "sha-two", factory=factory)
    assert not hit and second is not first and len(cache.entries) == 1
    third, hit = cache.get(tmp_path / "model.onnx", "sha-two", require_gpu=False, factory=factory)
    assert not hit and third is not second and len(calls) == 3
    cache.clear()
    assert not cache.entries
