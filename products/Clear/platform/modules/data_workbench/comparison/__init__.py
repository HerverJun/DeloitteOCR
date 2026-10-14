"""Explainable comparison of versioned tables."""

from .exact import CompareError, ComparisonResult, ComparisonSpec, compare_exact

__all__ = ["CompareError", "ComparisonResult", "ComparisonSpec", "compare_exact"]
