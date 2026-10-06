"""AGENT_CONTRACTS.md says what the code declares."""

from pathlib import Path
from typing import get_args

from ubo_sentinel.agents.base import Halt, PipelineState
from ubo_sentinel.agents.supervisor import PIPELINE

CONTRACTS_PATH = Path("AGENT_CONTRACTS.md")


def rows(heading):
    """The rows of the table whose header starts with `heading`, as lists of cells."""
    lines = CONTRACTS_PATH.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"| {heading} |"))
    found = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        found.append([cell.strip() for cell in line.strip("|").split("|")])
    return found


def fields(cell):
    return tuple(name.strip("` ") for name in cell.split(",")) if cell != "—" else ()


def test_the_contract_table_is_the_agents_declarations():
    documented = [
        (name, module.strip("`"), fields(requires), fields(provides), after_halt == "yes")
        for name, module, requires, provides, after_halt in rows("Agent")
    ]
    declared = [
        (
            agent.name,
            f"agents/{agent.__module__.rsplit('.', 1)[1]}.py",
            agent.requires,
            agent.provides,
            agent.runs_after_halt,
        )
        for agent in PIPELINE
    ]
    assert documented == declared


def test_every_contract_field_is_a_field_of_the_state():
    for agent in PIPELINE:
        assert set(agent.requires) | set(agent.provides) <= set(PipelineState.model_fields)
    # Each agent's output is some later agent's input, or the pipeline's result.
    provided = [name for agent in PIPELINE for name in agent.provides]
    assert len(provided) == len(set(provided))
    assert provided[-1] == "decision"


def test_the_halts_and_the_state_are_documented():
    assert {row[0].strip("`") for row in rows("`halt`")} == set(get_args(Halt))
    state_row = next(row for row in rows("Type") if row[0] == "`PipelineState`")
    documented = {name.strip("`. ") for name in state_row[2].split(".")[0].split(",")}
    assert documented == set(PipelineState.model_fields)
