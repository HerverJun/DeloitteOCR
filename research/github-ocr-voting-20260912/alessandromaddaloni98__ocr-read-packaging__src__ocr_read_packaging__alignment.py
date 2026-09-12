"""Needleman-Wunsch global sequence alignment adapted to OCR text.

Comparing two OCR readings character by character with an equality test is too strict:
one engine inserting a stray space, or splitting a word differently, shifts every
character after it and destroys the comparison. Global alignment solves that -- it finds
the best way to line the two strings up, absorbing insertions and deletions as gaps, and
the similarity is then the fraction of aligned positions that agree.

The scoring function additionally knows about character pairs that OCR routinely confuses
(``o``/``0``, ``l``/``1``, ...). See the note on :data:`SIMILAR_PENALTY` for what that does
and, importantly, what it does not do to the resulting score.
"""

from __future__ import annotations

import logging

import numpy as np

__all__ = [
    "GAP_PENALTY",
    "MATCH_SCORE",
    "MISMATCH_PENALTY",
    "SIMILAR_PENALTY",
    "align",
    "needleman_wunsch_similarity",
]

logger = logging.getLogger(__name__)


# Character pairs that a printing or scanning pipeline routinely confuses. A substitution
# within one of these pairs is scored at SIMILAR_PENALTY instead of MISMATCH_PENALTY,
# which makes the alignment prefer pairing those characters up over shifting the sequence.
# It does NOT make the pair count as a match -- see SIMILAR_PENALTY.
#
# Note on ("rn", "m"): the classic OCR failure where the ligature-like pair "rn" is read as
# "m" spans two characters on one side and one on the other, so a character-by-character
# scoring function can never look it up. The entry is kept because the alignment already
# handles the case adequately -- "rn" vs "m" aligns as one match plus one gap -- and
# because removing it would suggest the confusion is not known about.
_SIMILAR_CHARS: frozenset[frozenset[str]] = frozenset(
    {
        frozenset(("o", "0")),
        frozenset(("l", "1")),
        frozenset(("i", "l")),
        frozenset(("i", "1")),
        frozenset(("s", "5")),
        frozenset(("z", "2")),
        frozenset(("b", "6")),
        frozenset(("g", "9")),
        frozenset(("q", "9")),
        frozenset(("c", "e")),
        frozenset(("u", "v")),
        frozenset(("m", "n")),
        frozenset(("d", "b")),
        frozenset(("rn", "m")),
    }
)

MATCH_SCORE = 2.0
"""Score awarded when two aligned characters are identical."""

MISMATCH_PENALTY = -2.0
"""Score charged when two aligned characters genuinely differ."""

GAP_PENALTY = -1.0
"""Score charged for an insertion or a deletion."""

SIMILAR_PENALTY = -0.5
"""Score charged when two aligned characters are a known OCR confusion pair.

Cheaper than a mismatch, so the alignment prefers to pair confusable characters up rather
than treat them as unrelated and shift the sequence around them.

**This does not raise the similarity score for a substitution.** The score returned by
:func:`needleman_wunsch_similarity` counts aligned positions holding *identical*
characters, and a confusion pair is by definition not identical. Concretely,
``needleman_wunsch_similarity("lot0", "loto")`` and
``needleman_wunsch_similarity("lot0", "lotx")`` both return ``0.75``: the confusion matrix
changes neither result, because with MISMATCH_PENALTY equal to twice GAP_PENALTY the
alignment already prefers the diagonal in both cases.

The matrix therefore only matters for *which* alignment is chosen when a substitution
competes with a gap, and in practice it rarely changes the outcome. It is kept as-is to
preserve the behaviour this implementation was tuned against. Making it actually influence
the score -- by counting a confusion pair as a partial match -- is a plausible improvement,
but it shifts every score upward and the thresholds in
:class:`ocr_read_packaging.config.Thresholds` would need to be re-tuned to match.
"""

GAP_CHAR = "_"
"""Placeholder used in aligned output to mark an insertion or deletion."""


def _char_score(a: str, b: str) -> float:
    """Score a single aligned character pair."""
    if a == b:
        return MATCH_SCORE
    if frozenset((a.lower(), b.lower())) in _SIMILAR_CHARS:
        return SIMILAR_PENALTY
    return MISMATCH_PENALTY


def align(seq1: str, seq2: str) -> tuple[list[str], list[str]]:
    """Globally align two strings and return them character-aligned.

    Args:
        seq1: First string.
        seq2: Second string.

    Returns:
        A pair of equal-length character lists. Positions where one side has no
        counterpart hold :data:`GAP_CHAR`.

    Examples:
        >>> a, b = align("lot", "lo")
        >>> "".join(a), "".join(b)
        ('lot', 'lo_')
    """
    n, m = len(seq1), len(seq2)
    score = np.zeros((n + 1, m + 1), dtype=float)

    for i in range(1, n + 1):
        score[i][0] = score[i - 1][0] + GAP_PENALTY
    for j in range(1, m + 1):
        score[0][j] = score[0][j - 1] + GAP_PENALTY

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            diagonal = score[i - 1][j - 1] + _char_score(seq1[i - 1], seq2[j - 1])
            delete = score[i - 1][j] + GAP_PENALTY
            insert = score[i][j - 1] + GAP_PENALTY
            score[i][j] = max(diagonal, delete, insert)

    aligned1: list[str] = []
    aligned2: list[str] = []
    i, j = n, m
    while i > 0 or j > 0:
        if (
            i > 0
            and j > 0
            and score[i][j] == score[i - 1][j - 1] + _char_score(seq1[i - 1], seq2[j - 1])
        ):
            aligned1.append(seq1[i - 1])
            aligned2.append(seq2[j - 1])
            i -= 1
            j -= 1
        elif i > 0 and score[i][j] == score[i - 1][j] + GAP_PENALTY:
            aligned1.append(seq1[i - 1])
            aligned2.append(GAP_CHAR)
            i -= 1
        else:
            aligned1.append(GAP_CHAR)
            aligned2.append(seq2[j - 1])
            j -= 1

    aligned1.reverse()
    aligned2.reverse()
    return aligned1, aligned2


def needleman_wunsch_similarity(s1: str, s2: str, *, log_below: float | None = None) -> float:
    """Return the similarity of two strings as a value in ``[0, 1]``.

    The two strings are globally aligned, then the similarity is the fraction of aligned
    positions holding identical characters. Confusion-pair substitutions do not count as
    matches and do not raise the score; see :data:`SIMILAR_PENALTY`.

    Both inputs should already be normalised with
    :func:`ocr_read_packaging.normalize.normalize_text`.

    Args:
        s1: First string.
        s2: Second string.
        log_below: When given, comparisons scoring below this value are logged at debug
            level together with the first 40 characters of each input. Purely diagnostic.

    Returns:
        Similarity rounded to four decimals. Two empty strings score ``1.0``; one empty
        string against a non-empty one scores ``0.0``.

    Examples:
        >>> needleman_wunsch_similarity("lot 2024-a", "lot 2024-a")
        1.0
        >>> needleman_wunsch_similarity("", "")
        1.0
        >>> needleman_wunsch_similarity("abc", "")
        0.0
    """
    if not s1 and not s2:
        return 1.0
    if not s1 or not s2:
        return 0.0

    aligned1, aligned2 = align(s1, s2)
    total = len(aligned1)
    # strict=True: align() guarantees equal lengths, so a mismatch is a bug, not input.
    matches = sum(1 for a, b in zip(aligned1, aligned2, strict=True) if a == b)
    similarity = matches / total if total > 0 else 1.0

    if log_below is not None and similarity < log_below:
        logger.debug(
            "significant difference: score=%.3f | %r vs %r",
            similarity,
            s1[:40],
            s2[:40],
        )

    return round(similarity, 4)
