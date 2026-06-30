"""
text_normalizer.py
==================
Layer 1 of the hybrid parser.

Produces a clean, consistent representation of raw trade messages that is
safe to pass to the rule parser, entity extractor, and ML classifier.

Rules:
 - Strip leading/trailing whitespace.
 - Collapse internal whitespace runs to a single space.
 - Convert escaped newlines (\\n) and literal CR/LF to a single space.
 - Normalise Unicode dashes (en-dash, em-dash, figure dash) to a plain hyphen.
 - Normalise Unicode quotes to plain ASCII quotes.
 - Do NOT remove numeric values.
 - Do NOT remove the characters:  %  @  /  .  -  &  (  )  :
 - Preserve the original raw text separately.
 - Case is preserved for caller-controlled lowercasing.
"""

import re
import unicodedata


# Unicode characters that behave like hyphens
_UNICODE_DASHES = (
    "\u2010"  # HYPHEN
    "\u2011"  # NON-BREAKING HYPHEN
    "\u2012"  # FIGURE DASH
    "\u2013"  # EN DASH
    "\u2014"  # EM DASH
    "\u2015"  # HORIZONTAL BAR
    "\u2212"  # MINUS SIGN
    "\ufe58"  # SMALL EM DASH
    "\ufe63"  # SMALL HYPHEN-MINUS
    "\uff0d"  # FULLWIDTH HYPHEN-MINUS
)
_DASH_RE = re.compile(f"[{''.join(_UNICODE_DASHES)}]")

# Unicode curly / typographic quotes → plain ASCII
_QUOTE_MAP = str.maketrans(
    "\u2018\u2019\u201a\u201b\u2032\u2035",   # single variants
    "''''''",
)
_DQUOTE_MAP = str.maketrans(
    "\u201c\u201d\u201e\u201f\u2033\u2036",   # double variants
    '""""""',
)


def normalize(raw_text: str) -> str:
    """
    Return a normalised version of *raw_text* suitable for rule and ML
    processing.

    The original text is never modified in-place; a new string is returned.
    """
    if not raw_text or not isinstance(raw_text, str):
        return ""

    text = raw_text

    # 1. Unicode NFC so combining characters are canonical
    text = unicodedata.normalize("NFC", text)

    # 2. Replace escaped newlines and literal CR/LF/TAB with a space
    text = text.replace("\\n", " ").replace("\\r", " ").replace("\\t", " ")
    text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ")

    # 3. Normalise Unicode dashes to ASCII hyphen
    text = _DASH_RE.sub("-", text)

    # 4. Normalise typographic quotes
    text = text.translate(_QUOTE_MAP)
    text = text.translate(_DQUOTE_MAP)

    # 5. Collapse multiple spaces to one (do NOT strip meaningful chars)
    text = re.sub(r" {2,}", " ", text)

    # 6. Strip leading/trailing whitespace
    text = text.strip()

    return text


def normalize_for_matching(text: str) -> str:
    """Return normalised and uppercased text for case-insensitive rule matching."""
    return normalize(text).upper()


def normalize_for_ml(text: str) -> str:
    """Return normalised text for ML feature extraction (lowercase)."""
    return normalize(text).lower()
