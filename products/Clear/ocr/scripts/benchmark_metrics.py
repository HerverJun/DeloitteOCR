"""Explicit metrics; no spell correction, punctuation repair or model-specific cleanup."""

from collections import Counter
import re
import unicodedata


def normalize(text, whitespace=True):
    text = unicodedata.normalize("NFC", text)
    return "".join(text.split()) if whitespace else text


def distance(reference, hypothesis):
    if len(reference) < len(hypothesis):
        reference, hypothesis = hypothesis, reference
    row = list(range(len(hypothesis) + 1))
    for i, left in enumerate(reference, 1):
        next_row = [i]
        for j, right in enumerate(hypothesis, 1):
            next_row.append(
                min(next_row[-1] + 1, row[j] + 1, row[j - 1] + (left != right))
            )
        row = next_row
    return row[-1]


def text_metrics(reference, hypothesis):
    a, b = normalize(reference), normalize(hypothesis)
    strict_a, strict_b = normalize(reference, False), normalize(hypothesis, False)
    # Predeclared numeric-token fields; no inferred semantic field names.
    fields = Counter(re.findall(r"(?<![\d.])\d{4,}(?:\.\d+)?(?![\d.])", strict_a))
    predicted = Counter(re.findall(r"(?<![\d.])\d{4,}(?:\.\d+)?(?![\d.])", strict_b))
    return {
        "reference_characters": len(a),
        "edit_distance": distance(a, b),
        "strict_reference_characters": len(strict_a),
        "strict_edit_distance": distance(strict_a, strict_b),
        "exact": a == b,
        "numeric_fields": sum(fields.values()),
        "numeric_fields_correct": sum((fields & predicted).values()),
    }


def table_metrics(expected, predicted):
    def spans(tables):
        return {
            (i, c["row"], c["column"], c["row_span"], c["column_span"])
            for i, t in enumerate(tables)
            for c in t["cells"]
        }

    def cells(tables):
        return {
            (i, c["row"], c["column"]): normalize(c["text"])
            for i, t in enumerate(tables)
            for c in t["cells"]
        }

    a, b = spans(expected), spans(predicted)
    gt, pred = cells(expected), cells(predicted)
    dimensions = lambda ts: [(t["rows"], t["columns"]) for t in ts]
    return {
        "expected_cells": len(a),
        "predicted_cells": len(b),
        "correct_spans": len(a & b),
        "span_f1": 2 * len(a & b) / (len(a) + len(b)) if a or b else 1,
        "structure_exact": a == b and dimensions(expected) == dimensions(predicted),
        "text_cells_correct": sum(pred.get(k) == v for k, v in gt.items()),
        "numeric_fields": sum(bool(re.search(r"\d", v)) for v in gt.values()),
        "numeric_fields_correct": sum(
            bool(re.search(r"\d", v)) and pred.get(k) == v for k, v in gt.items()
        ),
    }
