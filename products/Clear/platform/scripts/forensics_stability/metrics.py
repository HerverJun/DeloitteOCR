"""Fixed forensic evaluation metrics; never product authenticity probabilities."""
from __future__ import annotations
import math
import numpy as np


def probability_map(value):
    value = np.asarray(value, dtype=np.float32)
    if value.ndim != 2 or not value.size or not np.isfinite(value).all() or value.min() < 0 or value.max() > 1:
        raise ValueError('Expected a finite, nonempty HxW response in [0,1]')
    return value


def top_one_percent(value):
    flat = probability_map(value).reshape(-1)
    count = max(1, math.ceil(flat.size / 100))
    return float(np.partition(flat, flat.size - count)[-count:].mean(dtype=np.float64))


def pixel_metrics(value, mask, threshold=.5):
    value = probability_map(value)
    truth = np.asarray(mask, dtype=bool)
    if truth.shape != value.shape:
        raise ValueError('Mask and response coordinate dimensions differ')
    predicted = value >= threshold
    tp = int(np.count_nonzero(predicted & truth))
    fp = int(np.count_nonzero(predicted & ~truth))
    fn = int(np.count_nonzero(~predicted & truth))
    positives = int(truth.sum())
    if not positives:
        return {'tp': tp, 'fp': fp, 'fn': fn, 'f1': None, 'iou': None, 'auprc': None,
                'normal_predicted_area_fraction': float(predicted.mean())}
    scores = value.reshape(-1)
    labels = truth.reshape(-1)
    order = np.argsort(-scores, kind='stable')
    sorted_scores = scores[order]
    cumulative = np.cumsum(labels[order], dtype=np.int64)
    ends = np.r_[np.flatnonzero(sorted_scores[1:] != sorted_scores[:-1]), scores.size - 1]
    true_positives = cumulative[ends]
    recall = true_positives / positives
    precision = true_positives / (ends + 1)
    average_precision = float(np.sum(np.diff(np.r_[0., recall]) * precision))
    return {'tp': tp, 'fp': fp, 'fn': fn, 'f1': 2 * tp / (2 * tp + fp + fn),
            'iou': tp / (tp + fp + fn), 'auprc': average_precision,
            'normal_predicted_area_fraction': None}


def calibrated_threshold(normal_scores):
    scores = np.sort(np.asarray(normal_scores, dtype=np.float64))
    if not scores.size or not np.isfinite(scores).all():
        raise ValueError('Calibration requires finite independent normal-family controls')
    # Empirical 95th percentile, higher order statistic. Ties are negative.
    return float(scores[min(scores.size - 1, math.ceil(.95 * (scores.size - 1)))])


def image_metrics(records, threshold):
    normals = [r for r in records if not r['is_modified']]
    modified = [r for r in records if r['is_modified']]
    return {'threshold': threshold, 'decision': 'score > threshold',
            'normal_images': len(normals), 'modified_images': len(modified),
            'false_positive_rate': sum(r['image_score'] > threshold for r in normals) / len(normals) if normals else None,
            'recall': sum(r['image_score'] > threshold for r in modified) / len(modified) if modified else None,
            'mean_modified_pixel_f1': float(np.mean([r['pixel']['f1'] for r in modified])) if modified else None,
            'mean_modified_pixel_iou': float(np.mean([r['pixel']['iou'] for r in modified])) if modified else None,
            'mean_modified_pixel_auprc': float(np.mean([r['pixel']['auprc'] for r in modified])) if modified else None}


def seam_band(shape, boundaries=(896, 1024), radius=64):
    height, width = shape
    result = np.zeros((height, width), dtype=bool)
    for boundary in boundaries:
        if 0 < boundary < width:
            result[:, max(0, boundary - radius):min(width, boundary + radius)] = True
    if not result.any():
        raise ValueError('Fixed seam comparison requires an image crossing the seam')
    return result


def acceptance(records, seam_records, peak_mib):
    by_mode = {mode: [r for r in records if r['mode'] == mode] for mode in ('legacy', 'halo64')}
    final_f1 = {mode: float(np.mean([r['pixel']['f1'] for r in rows if r['split'] == 'final_test' and r['is_modified']]))
                for mode, rows in by_mode.items()}
    legacy_mae = float(np.mean([r['legacy_seam_mae'] for r in seam_records]))
    halo_mae = float(np.mean([r['halo_seam_mae'] for r in seam_records]))
    reduction = (legacy_mae - halo_mae) / legacy_mae if legacy_mae > 0 else (0. if halo_mae == 0 else -1.)
    p95 = {mode: float(np.percentile([r['infer_seconds'] for r in rows], 95)) for mode, rows in by_mode.items()}
    ratio = p95['halo64'] / p95['legacy']
    checks = {'seam_mae_reduction_at_least_10_percent': reduction >= .10,
              'pixel_f1_drop_at_most_one_point': final_f1['halo64'] >= final_f1['legacy'] - .01,
              'p95_ratio_at_most_1_6': ratio <= 1.6,
              'gpu_peak_at_most_12_gib': 0 < peak_mib <= 12288}
    return {'enabled': all(checks.values()), 'checks': checks,
            'mean_legacy_seam_mae': legacy_mae, 'mean_halo_seam_mae': halo_mae,
            'seam_mae_reduction': reduction, 'final_test_pixel_f1': final_f1,
            'p95_seconds': p95, 'p95_ratio': ratio, 'sampled_peak_device_mib': peak_mib,
            'default_halo': 64 if all(checks.values()) else 0,
            'scope': 'Synthetic comparison; does not qualify business detection accuracy'}
