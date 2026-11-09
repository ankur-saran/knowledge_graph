"""Ownership extraction from analyst-supplied documents (Step 15).

`document` keeps a file and reads its text; `first_pass` parses holdings with no
model; `llm_extract` asks a model, with consent; `guardrail` checks every proposal
against the text it quotes; `run` labels them against the graph and records the run.
"""
