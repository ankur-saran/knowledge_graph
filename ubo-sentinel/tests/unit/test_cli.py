import pytest
import typer
import yaml
from typer.testing import CliRunner

from ubo_sentinel import __version__
from ubo_sentinel.cli.app import ONTOLOGY_PATH, app, require_role

runner = CliRunner()


@pytest.fixture
def ontology(tmp_path):
    """A copy of the shipped ontology in which only `analyst` may run `screen`."""
    data = yaml.safe_load(ONTOLOGY_PATH.read_text(encoding="utf-8"))
    data["roles"]["analyst"]["commands"] = ["screen"]
    path = tmp_path / "ontology.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Usage" in result.output


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_require_role_allows_permitted_command(ontology):
    require_role("analyst", "screen", ontology)


def test_require_role_denies_unpermitted_command(ontology):
    with pytest.raises(typer.Exit) as exc:
        require_role("engineer", "screen", ontology)
    assert exc.value.exit_code == 1


def test_require_role_rejects_unknown_role(ontology):
    with pytest.raises(typer.Exit) as exc:
        require_role("intern", "screen", ontology)
    assert exc.value.exit_code == 2


def test_require_role_fails_closed_without_ontology(tmp_path):
    with pytest.raises(typer.Exit) as exc:
        require_role("analyst", "screen", tmp_path / "missing.yaml")
    assert exc.value.exit_code == 2


def test_require_role_fails_closed_on_invalid_ontology(tmp_path):
    path = tmp_path / "ontology.yaml"
    path.write_text("roles:\n  analyst:\n    commands: [screen]\n", encoding="utf-8")
    with pytest.raises(typer.Exit) as exc:
        require_role("analyst", "screen", path)
    assert exc.value.exit_code == 2


def test_shipped_ontology_defines_the_four_roles():
    from ubo_sentinel.cli.app import load_permissions

    assert set(load_permissions()) == {"analyst", "reviewer", "auditor", "engineer"}


def test_the_ontology_can_be_named_by_the_environment(monkeypatch, tmp_path):
    from ubo_sentinel.models.ontology import DEFAULT_ONTOLOGY_PATH, ontology_path

    monkeypatch.delenv("UBO_ONTOLOGY", raising=False)
    assert ontology_path() == DEFAULT_ONTOLOGY_PATH
    monkeypatch.setenv("UBO_ONTOLOGY", str(tmp_path / "other.yaml"))
    assert ontology_path() == tmp_path / "other.yaml"


def test_ui_dry_run_names_a_local_server_and_the_role():
    from ubo_sentinel.cli.ui_cmd import APP_PATH

    result = runner.invoke(app, ["ui", "--role", "reviewer", "--port", "8600", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert APP_PATH.exists() and APP_PATH.name in result.output
    for words in ("--server.address localhost", "--browser.gatherUsageStats false",
                  "--server.port 8600", "-- --role reviewer"):  # fmt: skip
        assert words in result.output


def test_ui_starts_streamlit_from_here_with_the_ontology_named(monkeypatch):
    from ubo_sentinel.cli import ui_cmd

    started = {}

    def call(command, cwd, env):
        started.update(command=command, cwd=cwd, env=env)
        return 0

    monkeypatch.setattr(ui_cmd.subprocess, "call", call)
    result = runner.invoke(app, ["ui"])
    assert result.exit_code == 0, result.output
    assert started["command"][1:4] == ["-m", "streamlit", "run"]
    assert started["env"]["UBO_ONTOLOGY"] == str(ONTOLOGY_PATH.resolve())


def test_ui_is_refused_to_an_unknown_role():
    assert runner.invoke(app, ["ui", "--role", "intern", "--dry-run"]).exit_code == 2
