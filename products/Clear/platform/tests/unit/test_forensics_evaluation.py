"""Analytic counterexamples for evaluation metrics and leakage constraints."""
import importlib.util
from pathlib import Path
import numpy as np
import pytest

SOURCE = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('forensic_metrics_test', SOURCE/'scripts/forensics_stability/metrics.py')
metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metrics)


def test_perfect_and_reversed_predictions():
    truth = np.array([[False, True], [False, True]])
    perfect = metrics.pixel_metrics(truth.astype('float32'), truth)
    assert perfect['f1'] == perfect['iou'] == perfect['auprc'] == 1
    wrong = metrics.pixel_metrics((~truth).astype('float32'), truth)
    assert wrong['f1'] == wrong['iou'] == 0
    assert wrong['auprc'] == .5


def test_tied_predictions_do_not_depend_on_pixel_order():
    truth = np.array([[True, False], [True, False]])
    assert metrics.pixel_metrics(np.full((2,2),.7), truth)['auprc'] == .5
    assert metrics.pixel_metrics(np.full((2,2),.7), ~truth)['auprc'] == .5


def test_normal_controls_are_not_reported_as_perfect_localization():
    result = metrics.pixel_metrics(np.zeros((2,2)), np.zeros((2,2),bool))
    assert result['f1'] is result['iou'] is result['auprc'] is None
    assert result['normal_predicted_area_fraction'] == 0


def test_top_percent_uses_ceil_and_preserves_small_targets():
    value = np.zeros((10, 10), dtype='float32')
    value[0,0] = 1
    assert metrics.top_one_percent(value) == 1
    larger = np.zeros((1,101), dtype='float32')
    larger[0,0] = 1
    assert metrics.top_one_percent(larger) == .5


def test_calibration_ties_are_not_automatic_positives():
    threshold = metrics.calibrated_threshold([.2] * 48)
    result = metrics.image_metrics([{'is_modified':False,'image_score':.2}], threshold)
    assert result['false_positive_rate'] == 0


@pytest.mark.parametrize('value',[np.array([[np.nan]]),np.array([[-.1]]),np.array([[1.1]]),np.empty((0,0))])
def test_invalid_responses_are_rejected(value):
    with pytest.raises(ValueError):
        metrics.top_one_percent(value)


def test_mask_mismatch_is_not_silently_resized():
    with pytest.raises(ValueError):
        metrics.pixel_metrics(np.zeros((3,4)), np.zeros((4,3)))


def test_fixed_seam_band_includes_both_schemes():
    band = metrics.seam_band((896,1200))
    assert band[:,832:1088].all()
    assert not band[:,:832].any() and not band[:,1088:].any()
