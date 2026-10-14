"""Normalize publisher text before it reaches translation models."""

from collections import Counter
from html import unescape
import re


def readable_text(value):
    text = str(value or "")
    # JSON-LD sometimes contains HTML entities inside already escaped strings.
    for _ in range(3):
        decoded = unescape(text)
        if decoded == text:
            break
        text = decoded
    return text.replace("\xa0", " ").replace("\u200b", "").replace("\ufeff", "")


def model_text(value):
    text = readable_text(value).translate(
        str.maketrans(
            {
                "‘": "'",
                "’": "'",
                "“": '"',
                "”": '"',
                "«": '"',
                "»": '"',
                "–": "-",
                "—": "-",
                "…": "...",
            }
        )
    )
    return re.sub(r"(?<=\w)\s+'(?=\s|s\b)", "'", text)


def translation_defect(source, result):
    if not result.strip():
        return "empty translation"
    if re.search(r"<\s*/?(?:unk|s|pad)\s*>|\[UNK\]|[⟦⟧]|&\s*#\s*(?:\d+|x[\da-f]+)", result, re.I):
        return "technical tokens in translation"
    if "%" in result and "%" not in source and not re.search(r"\bper\s*cent", source, re.I):
        return "unexpected percentage in translation"
    if len(result) > max(len(source) * 3, len(source) + 200):
        return "excessive translation length"
    words = result.casefold().split()
    source_words = source.casefold().split()
    phrases = Counter(tuple(words[i : i + 5]) for i in range(len(words) - 4))
    originals = Counter(tuple(source_words[i : i + 5]) for i in range(len(source_words) - 4))
    for phrase, occurrences in phrases.items():
        original = originals[phrase]
        if occurrences >= 3 and occurrences > original:
            return "repeated text in translation"
    return None
