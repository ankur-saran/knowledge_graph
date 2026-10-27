# ubo-sentinel

Knowledge-graph-driven beneficial ownership and sanctions exposure screening.

Given an entity name or LEI, `ubo-sentinel` walks the ownership graph, applies the OFAC 50 Percent Rule by fix-point aggregation, writes a fully cited risk memo, and leaves the final decision to a human reviewer. It runs locally, needs no API keys, and is deterministic.

> **Not a production sanctions-screening system; not legal advice; rule packs must be validated by compliance counsel.**

The project lives in [`ubo-sentinel/`](ubo-sentinel/). Run every command from that directory.

## Status

Steps 0–11 of the build plan are done:

- ingest, normalise and build the graph (`ubo ingest`, `ubo normalize`, `ubo build-graph`), from synthetic fixtures or from real GLEIF and OpenSanctions data (`ubo refresh`);
- screen an entity into a recommendation with a cited memo (`ubo screen`);
- review it, verify the audit log and replay a decision (`ubo review`, `ubo audit verify`, `ubo audit replay`);
- do all of that in a local review UI with an evidence graph, as each role may (`ubo ui`);
- measure the PRD's success metrics (`ubo eval`, `ubo bench`), with unit and integration tests.

The re-screen queue (when a list changes, which decisions to screen again) is the next step.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```
cd ubo-sentinel
uv sync --all-extras
uv run ubo ingest --source fixtures
uv run ubo normalize
uv run ubo build-graph
uv run ubo screen "Acme Trading FZE"
uv run ubo ui                  # the review UI on http://localhost:8501
uv run pytest --cov            # the full suite with the coverage gate (same as: make test)
```

Runtime output (database, audit log, decisions, downloads) is written to `ubo-sentinel/var/`, which is gitignored.

## Documentation

| File | Contents |
|---|---|
| [ONTOLOGY.md](ubo-sentinel/ONTOLOGY.md) | Node labels, edge types, roles and permissions |
| [AGENT_CONTRACTS.md](ubo-sentinel/AGENT_CONTRACTS.md) | Typed input and output of each agent |
| [RUNBOOK.md](ubo-sentinel/RUNBOOK.md) | Demo script: the UI and the commands, step by step |
| [CLAUDE.md](ubo-sentinel/CLAUDE.md) | Conventions for building this repo with Claude Code |
| [fixtures/README.md](ubo-sentinel/fixtures/README.md) | The synthetic fixtures and their format |

The step-by-step build plan (`ubo-sentinel/BUILD_PLAN.md`) and the PRD are kept locally and are not in the repository.

## Data licences

- **OpenSanctions** data is licensed **CC BY-NC 4.0**. This project uses it for demo and personal use only; commercial use requires a licence from OpenSanctions.
- **GLEIF** LEI data is published under CC0.
- The files in `ubo-sentinel/fixtures/` are synthetic. Any resemblance to real entities is unintended.
