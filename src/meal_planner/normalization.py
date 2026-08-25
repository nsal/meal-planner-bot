"""Dependency-neutral normalization for food terms and evidence text."""

import unicodedata

NormalizedFood = tuple[str, ...]


def normalize_match_text(value: str) -> str:
    """Normalize Unicode, punctuation, and whitespace in text."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    characters = [
        character if character.isalnum() else " " for character in normalized
    ]
    return " ".join("".join(characters).split())


def _normalize_plural(token: str) -> str:
    """Apply conservative singular/plural normalization to one token."""
    if token.endswith("ies"):
        return token
    if len(token) > 2 and token.endswith("y") and token[-2] not in "aeiou":
        return f"{token[:-1]}ies"
    if len(token) > 2 and token.endswith("ie"):
        return f"{token}s"
    if (
        len(token) > 2
        and token.endswith("s")
        and not token.endswith(("ss", "us", "is"))
    ):
        return token[:-1]
    return token


def normalize_food(value: str) -> NormalizedFood:
    """Return normalized whole-word tokens for one food term or phrase."""
    normalized = normalize_match_text(value)
    return tuple(_normalize_plural(token) for token in normalized.split())
