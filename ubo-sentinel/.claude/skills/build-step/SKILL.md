---
name: build-step
description: Implement exactly one numbered step of the project's build plan, run that step's verification command, report the real output, and stop. Use when asked to build, implement or do "step N" of the build plan.
---

# Build one step of the build plan

**Input:** a step number (`$ARGUMENTS`). If none is given, ask for one and stop.

All project-specific names come from the `## Project facts` section of `CLAUDE.md` at the repository root. If that section is missing, or lacks a fact this procedure needs, say which one and stop.

## Procedure

1. Read `## Project facts` and the conventions in `CLAUDE.md`. Note the build-plan file, the package root, the test command and the lint command.
2. Open the build-plan file and read the whole section for the requested step, including its "Verifiable" line and every sub-step. Also read the plan's cross-cutting conventions.
3. Check the previous step is in place: the files it should have produced exist, and the test command passes. If not, report what is missing and stop.
4. Implement the requested step and nothing else.
   - Create only the files the step names, plus tests for them.
   - Do not start work belonging to a later step, even where it would be convenient. If the step cannot be finished without it, say so and stop.
   - Where the plan is ambiguous, choose the simplest reading and record the choice for the report.
5. Run the lint command and fix what it reports in the files you touched.
6. Run the step's "Verifiable" command exactly as the plan writes it. Then run the test command to confirm earlier steps still pass.
7. If a project fact changed (a path now exists, a command is now available), update `## Project facts` in `CLAUDE.md`.

## Done

The step's "Verifiable" command was run and produced the result the plan describes, and the test command passes.

## Report

- The step number and title
- Files created or changed
- Each verification command and its actual output, including failures; never describe a command you did not run as passing
- Choices made where the plan was ambiguous
- Anything the step called for that was not done, and why

Then stop. Do not begin the next step.
