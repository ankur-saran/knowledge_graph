# ubo-sentinel

Knowledge-graph-driven beneficial ownership and sanctions exposure screening.

Given an entity name or LEI, `ubo-sentinel` walks the ownership graph, applies the OFAC 50 Percent Rule by fix-point aggregation, writes a fully cited risk memo, and leaves the final decision to a human reviewer. It runs locally, needs no API keys, and is deterministic.

> **Not a production sanctions-screening system; not legal advice; rule packs must be validated by compliance counsel.**

## Status

Under construction. The build follows [BUILD_PLAN.md](BUILD_PLAN.md) one step at a time; Step 0 (project skeleton) is done. The CLI exists but has no commands yet.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```
uv sync --all-extras        # or: make install
uv run ubo --help
uv run pytest
```

Run every command from the repository root. Runtime output (database, audit log, decisions, downloads) is written to `var/`, which is gitignored.

## Documentation

| File | Contents |
|---|---|
| [BUILD_PLAN.md](BUILD_PLAN.md) | Step-by-step implementation plan |
| [ONTOLOGY.md](ONTOLOGY.md) | Node labels, edge types, roles and permissions |
| [AGENT_CONTRACTS.md](AGENT_CONTRACTS.md) | Typed input and output of each agent |
| [RUNBOOK.md](RUNBOOK.md) | Demo script |
| [CLAUDE.md](CLAUDE.md) | Conventions for building this repo with Claude Code |

## Data licences

- **OpenSanctions** data is licensed **CC BY-NC 4.0**. This project uses it for demo and personal use only; commercial use requires a licence from OpenSanctions.
- **GLEIF** LEI data is published under CC0.
- The files in `fixtures/` are synthetic. Any resemblance to real entities is unintended.
