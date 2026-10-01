import pytest
import typer
from typer.testing import CliRunner

from ubo_sentinel import __version__
from ubo_sentinel.cli.app import app, require_role

runner = CliRunner()


@pytest.fixture
def ontology(tmp_path):
    path = tmp_path / "ontology.yaml"
    path.write_text(
        "roles:\n  analyst:\n    commands: [screen]\n  engineer:\n    commands: []\n",
        encoding="utf-8",
    )
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


def test_shipped_ontology_defines_the_four_roles():
    from ubo_sentinel.cli.app import load_permissions

    assert set(load_permissions()) == {"analyst", "reviewer", "auditor", "engineer"}
