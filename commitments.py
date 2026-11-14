"""Judge whether two self-scheduled speaking commitments mean the same thing.

The model rewrites its own promise every round ("六点前后问一句今天怎么样"、
"约六点多自然问一句今天怎么样"), so exact-text deduplication never fires and
the same intention piles up as several forced openings. Everything here is pure
text/time comparison: no model calls, no state mutation.

Thresholds were calibrated on real commitments: the same intention said three
different ways scored 0.50-0.80 containment, unrelated promises scored 0.00-0.17.
"""

import re

# 包含度阈值：较长的那句里有这么大比例的内容出现在另一句里，就算同一件事
SIMILAR_THRESHOLD = 0.5
# 合并时要求至少共享这么多字符二元组，避免"六点问他"和"六点喝药"这种撞车
MIN_SHARED_BIGRAMS = 4
# 取消时允许更宽松：一句话带过的提醒（"提醒喝药"）也要能匹配上
CANCEL_THRESHOLD = 0.5
CANCEL_MIN_SHARED = 1
# 两条约定的时间差超过这个跨度，就当作两件不同的事，不合并
MERGE_WINDOW_SECONDS = 2 * 3600

_PUNCTUATION = re.compile(r"[^\w\u4e00-\u9fff]+")


def normalize(text) -> str:
    """Strip punctuation, spacing and case so wording differences stop counting.

    Args:
        text: Raw reason text.

    Returns:
        A compact comparison key.
    """
    return _PUNCTUATION.sub("", str(text or "")).lower()


def _bigrams(text: str) -> set[str]:
    """Split a comparison key into character bigrams.

    Args:
        text: Normalized text.

    Returns:
        The set of adjacent character pairs, or the whole text when it is a
        single character.
    """
    if len(text) < 2:
        return {text} if text else set()
    return {text[index : index + 2] for index in range(len(text) - 1)}


def _shared(first, second):
    """Return both bigram sets and their intersection.

    Args:
        first: One raw text.
        second: The other raw text.

    Returns:
        Tuple of (first set, second set, shared set).
    """
    left = _bigrams(normalize(first))
    right = _bigrams(normalize(second))
    return left, right, left & right


def similar(
    first,
    second,
    threshold: float = SIMILAR_THRESHOLD,
    min_shared: int = MIN_SHARED_BIGRAMS,
) -> bool:
    """Report whether two reasons describe the same intention.

    Uses containment rather than symmetric overlap: the model pads the same
    promise with extra clauses, which drags Jaccard down even though one text
    clearly contains the other's subject.

    Args:
        first: One reason text.
        second: The other reason text.
        threshold: Minimum containment to count as the same intention.
        min_shared: Minimum shared bigrams, guarding short accidental matches.

    Returns:
        True when the two reasons are the same thing said differently.
    """
    left_text, right_text = normalize(first), normalize(second)
    if not left_text or not right_text:
        return False
    if left_text == right_text:
        return True
    if left_text in right_text or right_text in left_text:
        return True
    left, right, shared = _shared(first, second)
    if not left or not right or len(shared) < min_shared:
        return False
    return len(shared) / min(len(left), len(right)) >= threshold


def same_intention(a, b, window: float = MERGE_WINDOW_SECONDS) -> bool:
    """Report whether two schedule entries are the same promise.

    Args:
        a: Existing entry with ``at`` and ``reason``.
        b: Candidate entry with ``at`` and ``reason``.
        window: Maximum seconds between the two moments to still merge.

    Returns:
        True when both fall inside the window and share the same intention.
    """
    try:
        close_in_time = abs(float(a.get("at", 0)) - float(b.get("at", 0))) <= window
    except (TypeError, ValueError):
        close_in_time = False
    if not close_in_time:
        return False
    return similar(a.get("reason", ""), b.get("reason", ""))


def find_same(slots, candidate, window: float = MERGE_WINDOW_SECONDS):
    """Locate the existing entry that the candidate duplicates.

    Args:
        slots: Existing schedule entries.
        candidate: Entry about to be added.
        window: Maximum seconds between the two moments to still merge.

    Returns:
        The duplicated entry, or None when the candidate is new.
    """
    for item in slots:
        if same_intention(item, candidate, window):
            return item
    return None


def score(entry, reason) -> float:
    """Score how strongly a stored entry matches a cancellation description.

    Args:
        entry: Stored schedule entry.
        reason: Short description of the promise to cancel.

    Returns:
        Containment ratio in 0..1; 1.0 also for substrings and equal text.
    """
    left_text, right_text = normalize(entry.get("reason", "")), normalize(reason)
    if not left_text or not right_text:
        return 0.0
    if left_text == right_text or left_text in right_text or right_text in left_text:
        return 1.0
    left, right, shared = _shared(entry.get("reason", ""), reason)
    if not left or not right:
        return 0.0
    return len(shared) / min(len(left), len(right))


def best_match(slots, reason, threshold: float = CANCEL_THRESHOLD):
    """Pick the single entry a cancellation most likely refers to.

    One instruction cancels one promise: picking only the best match keeps a
    loosely worded "问他下工" from wiping an unrelated neighbouring entry.

    Args:
        slots: Existing schedule entries.
        reason: Short description of the promise to cancel.
        threshold: Minimum score to accept any entry at all.

    Returns:
        The matching entry, or None when nothing is close enough.
    """
    best = None
    best_score = 0.0
    for item in slots:
        current = score(item, reason)
        if current > best_score:
            best, best_score = item, current
    if best is None or best_score < threshold:
        return None
    return best


def matches_reason(entry, reason, threshold: float = CANCEL_THRESHOLD) -> bool:
    """Report whether a stored entry is the one a cancellation refers to.

    Args:
        entry: Stored schedule entry.
        reason: Short description of the promise to cancel.
        threshold: Minimum containment to count as a match.

    Returns:
        True when the entry should be dropped.
    """
    return similar(
        entry.get("reason", ""), reason, threshold, min_shared=CANCEL_MIN_SHARED
    )
