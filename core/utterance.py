"""Is the user still in the middle of a sentence?

Seen live: Krishna pauses mid-sentence ("I'm very hungry. So", "and the concept is", "write a prompt in
Claude. Saying") and Gemini answered every piece, usually with a question, or acted on half a request
(asking the job agents about "some irrelevant jobs ... which are like really irrelevant to"). Words that
end on one of these are still going, and so is a lone stray word ("the", "A").

Words that end a sentence ("Okay, give me a prompt for that.", "I think so.", "...cooler than that?") are
finished whatever their last word is, and so are words ending on a number ("60 to 70%"). Seen live 28 Oct:
without this, ten complete questions in one evening waited for more words that never came, until Krishna
asked "You there?".
"""

from __future__ import annotations

import re

DANGLING = {
    "and", "so", "but", "or", "nor", "because", "cause", "like", "then", "if", "when", "while", "which", "that",
    "who", "where", "whether", "the", "a", "an", "my", "your", "our", "their", "his", "her", "its", "this",
    "these", "those", "to", "of", "for", "with", "from", "in", "on", "at", "by", "about", "into", "saying",
    "says", "is", "are", "was", "were", "be", "um", "uh", "ahm", "umm", "er", "un",
}


def unfinished(text: str) -> bool:
    """True if the words stop mid-sentence: the user is still talking."""
    end = text.strip().rstrip("\"')]»” ")
    if not end:
        return False
    if end[-1] in ".?!" and not end.endswith(".."):   # "and then..." trails off: the words decide
        return False
    words = re.findall(r"[a-z0-9%']+", end.lower())
    return bool(words) and words[-1] in DANGLING
