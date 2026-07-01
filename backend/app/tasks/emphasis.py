from typing import Optional

import nltk

# Content-word POS tags (nouns, verbs, adjectives) — the words worth visually
# emphasizing. Everything else (articles, prepositions, pronouns, conjunctions)
# is filler for this purpose.
_EMPHASIS_TAGS = {
    "NN", "NNS", "NNP", "NNPS",
    "VB", "VBD", "VBG", "VBN", "VBP", "VBZ",
    "JJ", "JJR", "JJS",
}


def pick_emphasis_word(words: list[str]) -> Optional[int]:
    """Return the index of the word to visually emphasize in this group, or
    None if the group has no noun/verb/adjective to highlight.

    Tags the group with NLTK's averaged perceptron tagger and picks the
    longest content word as the semantic focal point — matches how real
    caption apps pop a keyword mid-phrase rather than a fixed position.
    """
    if not words:
        return None
    tagged = nltk.pos_tag(words)
    candidates = [i for i, (_, tag) in enumerate(tagged) if tag in _EMPHASIS_TAGS]
    if not candidates:
        return None
    return max(candidates, key=lambda i: len(words[i]))
