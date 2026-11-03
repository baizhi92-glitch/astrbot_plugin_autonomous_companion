"""Natural paragraph segmentation for proactive plain text."""

import re


def split_proactive_text(text, limit=300):
    if len(text) <= limit and not re.search(r"\n\s*\n", text):
        return [text]
    # Keep fenced code intact; it should remain copyable.
    if "```" in text:
        return [text]
    segments = []
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        while len(paragraph) > limit:
            candidates = [
                m.end() for m in re.finditer(r"[。！？!?；;]\s*|\n", paragraph[:limit])
            ]
            cut = candidates[-1] if candidates else limit
            segments.append(paragraph[:cut].strip())
            paragraph = paragraph[cut:].strip()
        if paragraph:
            segments.append(paragraph)
    return segments or [text]
