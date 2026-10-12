"""R2.6B: `scripts/copilot_live_eval.py`, run end to end with NO provider and NO network.

The provider factory is replaced by a stand-in model, sockets are disabled for every test, and
the settings are built in the test with a sentinel key. What is checked is the script's own
behaviour: what it refuses and when (before any client exists), what identity it records for the
cases it ran, which model ids it records, and that the key appears nowhere it writes or prints.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import socket
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.core.config import Settings
from app.llm.base import ChatRequest, ChatResponse, TokenUsage
from tests.evaluation.knowledge_questions import COPILOT_KNOWLEDGE_EVAL_V1
from tests.evaluation.questions import COPILOT_EVAL_V1

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "copilot_live_eval.py"
SENTINEL_KEY = "sk-test-sentinel-never-written-4b1d"
CONFIGURED = "configured-model-id"
RETURNED = "provider-returned-model-id"
TODAY = dt.date(2026, 10, 12)


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("copilot_live_eval", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StandInModel:
    """Answers every request without tools. `returned` is what a provider would name, or None."""

    def __init__(self, returned: str | None) -> None:
        self.returned = returned
        self.calls = 0

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.calls += 1
        return ChatResponse(
            text="Nothing to report.",
            parsed=None,
            usage=TokenUsage(input_tokens=3, output_tokens=2),
            latency_ms=0.0,
            provider="anthropic",
            model=CONFIGURED,
            finish_reason="stop",
            prompt_id=request.prompt_id,
            prompt_version=request.prompt_version,
            diagnostics={} if self.returned is None else {"response_model": self.returned},
        )


class World:
    def __init__(self, script: ModuleType, out: Path) -> None:
        self.script = script
        self.out = out
        self.built: list[StandInModel] = []
        self.returned: str | None = RETURNED

    def run(self, *extra: str) -> int:
        return int(
            self.script.main(
                ["--out", str(self.out), "--confirm-paid-api-call", *extra], today=lambda: TODAY
            )
        )

    def files(self) -> tuple[dict[str, Any], dict[str, Any]]:
        replays = json.loads((self.out / f"replays-{TODAY}.json").read_text(encoding="utf-8"))
        report = json.loads((self.out / f"report-{TODAY}.json").read_text(encoding="utf-8"))
        return replays, report


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a test attempted to open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> World:
    import app.llm.factory as factory

    script = load_script()
    state = World(script, tmp_path / "run-1")
    monkeypatch.setattr(
        script,
        "load_settings",
        lambda: Settings(
            _env_file=None,
            environment="test",
            llm_enabled=True,
            llm_api_key=SENTINEL_KEY,
            llm_model=CONFIGURED,
        ),
    )

    def build(_settings: Settings) -> StandInModel:
        model = StandInModel(state.returned)
        state.built.append(model)
        return model

    monkeypatch.setattr(factory, "build_chat_model", build)
    return state


# --- selection identity --------------------------------------------------------------------------


def test_a_full_set_run_records_the_full_set_identically_in_both_files(world: World) -> None:
    assert world.run("--set", "copilot_knowledge_eval_v1") == 0

    replays, report = world.files()
    full = COPILOT_KNOWLEDGE_EVAL_V1
    selection = replays["live_provenance"]["selection"]
    assert replays["question_set_checksum"] == report["question_set"]["checksum"] == full.checksum
    assert selection == report["live_provenance"]["selection"]
    assert selection["subset"] is False
    assert selection["case_ids"] == [case.case_id for case in full.cases]
    assert selection["selected_set_checksum"] == selection["full_set_checksum"] == full.checksum
    assert sorted(replays["cases"]) == sorted(case.case_id for case in full.cases)


def test_a_one_case_run_records_the_subset_checksum_in_both_files(world: World) -> None:
    assert world.run("--case", "kpis_may_occupancy") == 0

    replays, report = world.files()
    selection = replays["live_provenance"]["selection"]
    assert selection["subset"] is True
    assert selection["case_ids"] == ["kpis_may_occupancy"]
    assert replays["question_set_checksum"] == report["question_set"]["checksum"]
    assert replays["question_set_checksum"] == selection["selected_set_checksum"]
    assert selection["selected_set_checksum"] != COPILOT_EVAL_V1.checksum
    assert selection["full_set_checksum"] == COPILOT_EVAL_V1.checksum
    assert selection["full_set_case_count"] == 24
    assert list(replays["cases"]) == ["kpis_may_occupancy"]


def test_several_cases_are_recorded_in_the_sets_order_with_one_checksum(world: World) -> None:
    assert world.run("--case", "guest_names", "--case", "kpis_may_occupancy") == 0

    replays, report = world.files()
    selection = report["live_provenance"]["selection"]
    assert selection["case_ids"] == ["kpis_may_occupancy", "guest_names"]
    assert selection["selected_case_count"] == 2
    assert replays["question_set_checksum"] == report["question_set"]["checksum"]
    assert replays["question_set_checksum"] == selection["selected_set_checksum"]
    assert replays["live_provenance"] == report["live_provenance"]


def test_an_unknown_case_is_refused_before_a_client_exists(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert world.run("--case", "no_such_case") == 2
    assert world.built == []
    assert "no_such_case" in capsys.readouterr().err


# --- configured versus returned model, and the SDK ----------------------------------------------


def test_the_configured_and_returned_model_ids_are_recorded_separately(world: World) -> None:
    assert world.run("--case", "kpis_may_occupancy") == 0

    _, report = world.files()
    provenance = report["live_provenance"]
    assert provenance["configured_model"] == CONFIGURED
    assert provenance["returned_models"] == [RETURNED]
    assert provenance["returned_models_by_case"]["kpis_may_occupancy"] == [RETURNED]


def test_a_response_naming_no_model_is_recorded_as_unknown_not_as_the_configuration(
    world: World,
) -> None:
    world.returned = None
    assert world.run("--case", "kpis_may_occupancy") == 0

    replays, report = world.files()
    for provenance in (replays["live_provenance"], report["live_provenance"]):
        assert provenance["returned_models"] == ["UNKNOWN"]
        assert CONFIGURED not in provenance["returned_models_by_case"]["kpis_may_occupancy"]
        assert provenance["configured_model"] == CONFIGURED


def test_the_sdk_version_is_recorded_from_metadata_or_as_unknown(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.llm.providers import anthropic_provider

    monkeypatch.setattr(anthropic_provider, "installed_sdk_version", lambda: None)
    assert world.run("--case", "kpis_may_occupancy") == 0
    _, report = world.files()
    assert report["live_provenance"]["sdk_version"] == "UNKNOWN"
    assert report["live_provenance"]["sdk_distribution"] == "anthropic"

    monkeypatch.setattr(anthropic_provider, "installed_sdk_version", lambda: "0.42.0")
    world.out = tmp_path / "run-2"
    assert world.run("--case", "kpis_may_occupancy") == 0
    _, report = world.files()
    assert report["live_provenance"]["sdk_version"] == "0.42.0"


# --- never overwrite --------------------------------------------------------------------------


@pytest.mark.parametrize("existing", ["replays", "report"])
def test_an_existing_output_is_refused_before_a_client_exists(
    world: World, existing: str, capsys: pytest.CaptureFixture[str]
) -> None:
    world.out.mkdir(parents=True)
    previous = world.out / f"{existing}-{TODAY}.json"
    previous.write_text("an earlier run", encoding="utf-8")

    assert world.run("--case", "kpis_may_occupancy") == 2

    assert world.built == []
    assert previous.read_text(encoding="utf-8") == "an earlier run"
    assert sorted(p.name for p in world.out.iterdir()) == [previous.name]
    assert "new --out" in capsys.readouterr().err


def test_writing_never_replaces_a_file_that_appeared_after_the_check(tmp_path: Path) -> None:
    script = load_script()
    target = tmp_path / "report.json"
    target.write_text("earlier", encoding="utf-8")

    with pytest.raises(FileExistsError):
        script.write_new(target, "later")
    assert target.read_text(encoding="utf-8") == "earlier"


# --- outside the repository --------------------------------------------------------------------


def test_an_output_inside_the_repository_is_refused_before_a_client_exists(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.out = REPO_ROOT / "eval-output-must-not-be-created"

    assert world.run("--case", "kpis_may_occupancy") == 2

    assert world.built == []
    assert not world.out.exists()
    assert "inside the repository" in capsys.readouterr().err


@pytest.mark.parametrize(
    "relative",
    [".", "eval-output", "tests/evaluation", "scripts/../eval-output"],
)
def test_every_path_inside_the_repository_is_recognised(relative: str) -> None:
    script = load_script()
    assert script.inside_repository(REPO_ROOT / relative)


def test_a_sibling_whose_name_extends_the_repositorys_is_outside_it(tmp_path: Path) -> None:
    script = load_script()
    root = tmp_path / "repo"
    assert not script.inside_repository(tmp_path / "repo-eval", root)
    assert not script.inside_repository(tmp_path / "copilot-eval" / "run", root)
    assert script.inside_repository(root / "out", root)


def test_case_differences_match_the_platforms_rules(tmp_path: Path) -> None:
    script = load_script()
    root = tmp_path / "Repo"
    variant = Path(str(root).swapcase()) / "out"
    assert script.inside_repository(variant, root) is (os.name == "nt")


# --- the key travels nowhere ------------------------------------------------------------------


def test_the_key_is_in_no_file_and_no_output(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert world.run("--case", "kpis_may_occupancy", "--case", "inject_other_hotel") == 0

    printed = capsys.readouterr()
    for path in world.out.iterdir():
        assert SENTINEL_KEY not in path.read_text(encoding="utf-8")
    assert SENTINEL_KEY not in printed.out
    assert SENTINEL_KEY not in printed.err


def test_a_refused_run_prints_no_key(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.out = REPO_ROOT / "eval-output-must-not-be-created"
    world.run()
    assert SENTINEL_KEY not in "".join(capsys.readouterr())


def test_a_disabled_configuration_is_refused_before_a_client_exists(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(world.script, "load_settings", lambda: None)

    assert world.run() == 2
    assert world.built == []
    assert "LLM_ENABLED must be true" in capsys.readouterr().err


def test_the_paid_call_flag_is_required() -> None:
    script = load_script()
    with pytest.raises(SystemExit):
        script.parse_arguments(["--out", "x"])


def test_a_subset_capture_cannot_pass_as_a_recording_of_the_full_set(world: World) -> None:
    from tests.evaluation.replay import load_replays

    assert world.run("--case", "kpis_may_occupancy") == 0

    with pytest.raises(ValueError, match="another question set"):
        load_replays(world.out / f"replays-{TODAY}.json", COPILOT_EVAL_V1)
