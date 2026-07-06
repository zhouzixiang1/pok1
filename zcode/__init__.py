"""zcode bot package — equity + pot-odds EV school.

A self-contained heads-up No-Limit Texas Hold'em bot implemented from first
principles (Monte-Carlo equity + pot-odds expected value), deliberately not
derived from the existing ``bots/bot*`` heuristic bots.

Modules
-------
cards          card encoding + best-5-of-7 hand evaluation
equity         Monte-Carlo win/tie estimation
state          protocol parsing (raise-to-total correct)
policy         EV-based decision policy
main           JSON (Botzone / local engine) entry point
national_bot   national TCP-platform entry point
"""

__all__ = ["cards", "equity", "state", "policy", "main"]
