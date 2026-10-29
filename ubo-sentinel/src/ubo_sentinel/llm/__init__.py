"""Optional, advisory LLM assists (Step 13): one client, one egress, one record.

Nothing here runs inside a screen. Every feature is off unless its flag is on,
reads only the analyst-masked view of a decision, is checked by code against
its input, and is recorded in the audit log as an `ANNOTATED` event (or, for
intake, an `INTAKE_ASSISTED` run). No output can change a recommendation, a
status or the canonical payload.
"""
