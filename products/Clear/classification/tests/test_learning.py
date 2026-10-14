import numpy as np
import pytest

from image_platform.learning import fit_logistic_head
from image_platform.runtime import probabilities


@pytest.mark.parametrize("classes", [2, 3])
def test_regularized_head_ignores_test_labels_and_exports_equivalent_logits(classes):
    rng = np.random.default_rng(123)
    targets = np.repeat(np.arange(classes), 10)
    features = np.eye(classes)[targets] + rng.normal(0, 0.03, (len(targets), classes))
    train = np.tile(np.arange(10) < 5, classes)
    val = np.tile((np.arange(10) >= 5) & (np.arange(10) < 8), classes)
    test = ~(train | val)
    fitted = fit_logistic_head(features, targets, train, val, classes)
    scores = probabilities(features.astype(np.float32) @ fitted["weight"].T + fitted["bias"])
    assert np.array_equal(scores.argmax(axis=1), targets)
    assert np.allclose(scores.sum(axis=1), 1)
    changed_features, changed_targets = features.copy(), targets.copy()
    changed_features[test] = 10000
    changed_targets[test] = 999
    other = fit_logistic_head(changed_features, changed_targets, train, val, classes)
    np.testing.assert_array_equal(fitted["weight"], other["weight"])
    np.testing.assert_array_equal(fitted["bias"], other["bias"])
    assert fitted["selected_c"] == other["selected_c"]


def test_regularization_rejects_overlapping_selection_data():
    with pytest.raises(ValueError, match="overlap"):
        fit_logistic_head(np.eye(2), np.array([0, 1]), np.array([True, True]), np.array([True, True]), 2)
