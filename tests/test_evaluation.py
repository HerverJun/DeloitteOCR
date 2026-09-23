import numpy as np
import pytest

from image_platform.evaluation import decision_metrics, wilson_interval


def test_small_accepted_sample_does_not_imply_precision_guarantee():
    lower, upper = wilson_interval(3, 3)
    assert lower == pytest.approx(0.4385, abs=0.0001)
    assert upper == pytest.approx(1)
    assert wilson_interval(0, 0) is None


def test_disabled_threshold_and_errors_in_accepted_set():
    result = decision_metrics(np.array([[20, 0], [0, 20], [0, 20]]), np.array([0, 1, 0]), ['a', 'b'], {'a': 1.0, 'b': 0.9})
    assert result['accepted_count'] == 2
    assert result['accepted_correct'] == 1
    assert result['accepted_precision'] == 0.5
    assert result['coverage'] == pytest.approx(2/3)
    assert result['thresholds_disabled_count'] == 1
