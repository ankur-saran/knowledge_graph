---
name: add-data-source
description: Add a new data source to a Bronze/Silver/Gold medallion pipeline - loader, Bronze table, Silver mapping with provenance, a small fixture sample, tests, and registration in the ingest command. Use when asked to add, onboard or ingest a new source.
---

# Add a data source to the medallion pipeline

**Input:** a source name (`$ARGUMENTS`), and where its data comes from (a file format and a sample, or a URL). If either is missing, ask and stop.

All project-specific names come from the `## Project facts` section of `CLAUDE.md` at the repository root: package root, loader directory, Bronze module, Silver module, Silver mapping module, fixture directory, ingest command, test command, lint command. If the section is missing, or a fact this procedure needs is missing or marked as not yet created, say which one and stop.

## Procedure

1. Read `## Project facts` and the conventions in `CLAUDE.md`, then read one existing loader and its tests. The new source follows that loader's structure, naming and provenance handling.
2. **Fixture sample.** Add a small sample of the source's raw format (tens of rows) under the fixture directory. It must be synthetic or freely licensed, and must cover every record type the loader handles and at least one malformed row.
3. **Loader.** Add a loader module in the loader directory that reads the raw format into Bronze. It must not open a network connection; downloading is a separate concern from loading.
4. **Bronze.** Add or extend the Bronze table(s) in the Bronze module. Keep the raw record and fill every ingestion-metadata column the existing tables carry. Loading the same input twice must add no rows.
5. **Silver mapping.** Map the Bronze rows to the existing canonical Silver tables in the Silver mapping module. Every Silver row carries full provenance. Add a canonical column only if no existing one fits, and say so in the report.
6. **Register.** Add the source name to the ingest command's accepted sources.
7. **Tests.** Add unit tests for: schema and row counts after loading the sample; idempotent re-load; provenance present on every Silver row; the malformed row is rejected or recorded, never silently dropped.
8. Run the lint command, then the test command.
9. Update `## Project facts` only if a path or command changed.

## Done

The ingest command accepts the new source and loads the fixture sample; the new tests and all existing tests pass.

## Report

- Files created or changed
- The ingest command run on the sample and its actual output
- The test command and its actual result
- Any canonical column added, and any source field deliberately left unmapped

This skill does not download real data, change decision rules, or add evaluation scenarios. If the source needs one of those, say so and stop.
