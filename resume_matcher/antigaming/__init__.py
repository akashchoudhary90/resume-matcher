"""Anti-gaming: detect resume manipulation (prompt injection, keyword stuffing, hidden text).

Every signal here is ADVISORY — it produces a flag for human review, never an automatic rejection
(plan §D). We deliberately do NOT run an 'AI-written?' text classifier: those are unreliable on short
text and biased against non-native English writers, which is its own disparate-impact problem.
"""
