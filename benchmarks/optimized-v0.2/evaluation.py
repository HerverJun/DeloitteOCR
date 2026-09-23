"""Descriptive held-out metrics; never used to select training parameters."""
import math

import numpy as np

from .runtime import decide


def wilson_interval(correct, count, z=1.959963984540054):
    if not count:
        return None
    p = correct / count
    denominator = 1 + z * z / count
    center = (p + z * z / (2 * count)) / denominator
    radius = z * math.sqrt(p * (1 - p) / count + z * z / (4 * count * count)) / denominator
    return [max(0.0, center - radius), min(1.0, center + radius)]


def decision_metrics(logits, target, label_ids, thresholds):
    decisions = decide(logits, {"labels": [{"label_id": label, "class_index": i, "display_name": label} for i, label in enumerate(label_ids)],
                                "decision_policy": {"thresholds_by_label_id": thresholds}}, True)
    correct = np.argmax(logits, axis=1) == target
    accepted = np.array([row["status"] == "accepted" for row in decisions])
    count, accepted_count = len(target), int(accepted.sum())
    accepted_correct = int(correct[accepted].sum())
    return {"accuracy_ci95_wilson": wilson_interval(int(correct.sum()), count),
            "accepted_count": accepted_count, "accepted_correct": accepted_correct,
            "coverage": accepted_count / count if count else 0.0,
            "accepted_precision": accepted_correct / accepted_count if accepted_count else None,
            "accepted_precision_ci95_wilson": wilson_interval(accepted_correct, accepted_count),
            "thresholds_disabled_count": sum(value >= 1.0 for value in thresholds.values()),
            "interval_scope": "Descriptive binomial interval on this fixed split; not a guarantee or correction for validation selection."}
