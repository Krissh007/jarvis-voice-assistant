"""Is the user still in the middle of a sentence?

Seen live: Krishna pauses mid-sentence ("I'm very hungry. So", "and the concept is", "write a prompt in
Claude. Saying") and Gemini answered every piece, usually with a question, or acted on half a request
(asking the job agents about "some irrelevant jobs ... which are like really irrelevant to"). Words that
end on one of these are still going, and so is a lone stray word ("the", "A").
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
    words = re.findall(r"[a-z']+", text.lower())
    return bool(words) and words[-1] in DANGLING
