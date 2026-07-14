# Archived Botzone/local JSON stack

This tree is historical, non-authoritative code. It contains the former
stdin/stdout JSON battle engine, its web-evolution fixtures, the JSON-to-TCP
adapter, Botzone operator scripts, and the former `web/core/reference_bots`
strategy corpus.

`ref/` contains the retired Botzone player API, old HTML reference, and the
unpopulated DanLM/neuron-poker gitlinks. They have no current strategy,
protocol, testing, or prompt-evidence authority.

The active project has exactly one competition/evolution protocol: the raw
national TCP protocol implemented by `sever/`, exercised by
`web/core/national_native.py`, and certified against the official Windows EXE.
Code in this archive must not affect candidate generation, quality gates,
precommit strength, ratings, opponent evidence, or official certification.

Historical/RL code may import it explicitly through the full package name
`archive.botzone_local...`. Do not put this directory on a production
`PYTHONPATH`, do not add compatibility shims named `engine`, and do not restore
adapter-backed evidence to the active results tree.
