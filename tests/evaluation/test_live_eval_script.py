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
        #: None: `LLM_THINKING` not configured, so the settings default applies.
        self.thinking: str | None = None
        #: The provider the settings select, and the consent flag a run passes (R2.7O).
        self.provider = "anthropic"
        self.consent = "--confirm-paid-api-call"

    def run(self, *extra: str) -> int:
        return int(
            self.script.main(["--out", str(self.out), self.consent, *extra], today=lambda: TODAY)
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
    monkeypatch.delenv("LLM_THINKING", raising=False)  # the parent shell must not decide it

    def configured() -> Settings:
        extra: dict[str, Any] = {} if state.thinking is None else {"llm_thinking": state.thinking}
        if state.provider == "anthropic":
            # Most tests here exercise the paid path; the local one has its own below.
            extra.update(llm_api_key=SENTINEL_KEY, llm_model=CONFIGURED)
        return Settings(
            _env_file=None,
            environment="test",
            llm_enabled=True,
            llm_provider=state.provider,
            **extra,
        )

    monkeypatch.setattr(script, "load_settings", configured)

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


def test_the_actually_installed_sdk_version_is_recorded(world: World) -> None:
    """Unpatched: whatever package metadata says is installed here, or `UNKNOWN` (R2.7F)."""
    import importlib.metadata

    try:
        expected = importlib.metadata.version("anthropic")
    except importlib.metadata.PackageNotFoundError:
        expected = "UNKNOWN"

    assert world.run("--case", "kpis_may_occupancy") == 0
    replays, report = world.files()
    assert replays["live_provenance"]["sdk_version"] == expected
    assert report["live_provenance"]["sdk_version"] == expected


def test_an_unconfigured_run_records_the_safe_default(world: World) -> None:
    """R2.7I: with `LLM_THINKING` unset, the capture records `disabled` -- what was sent."""
    assert world.run("--case", "kpis_may_occupancy") == 0

    replays, report = world.files()
    assert replays["live_provenance"]["thinking"] == "disabled"
    assert report["live_provenance"]["thinking"] == "disabled"


def test_custom_headers_in_the_environment_refuse_the_run_before_a_client_exists(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The script's REAL `load_settings`, run from an empty directory so no `.env` is read."""
    from app.core.config import PROVIDER_HEADERS_REFUSAL

    script = load_script()  # unpatched: the world fixture replaced the other copy's loader
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_API_KEY", SENTINEL_KEY)
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "X-Probe-R27i: SENTINEL-HEADER-VALUE-9f3")

    code = script.main(
        ["--out", str(tmp_path / "out"), "--confirm-paid-api-call", "--case", "kpis_may_occupancy"],
        today=lambda: TODAY,
    )

    printed = capsys.readouterr()
    assert code == 2
    assert world.built == []
    assert PROVIDER_HEADERS_REFUSAL in printed.err
    for leaked in ("SENTINEL-HEADER-VALUE-9f3", "X-Probe-R27i", SENTINEL_KEY):
        assert leaked not in printed.err
        assert leaked not in printed.out
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("consent", ["--confirm-paid-api-call", "--confirm-local-model-call"])
def test_a_key_beside_an_implicit_provider_refuses_the_run_before_a_client_exists(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    consent: str,
) -> None:
    """R2.7V. The script's REAL `load_settings`, from an empty directory so no `.env` is read:
    the fixed sentence is printed -- not the generic key sentence -- whichever flag is passed."""
    from app.core.config import IMPLICIT_PROVIDER_REFUSAL

    script = load_script()  # unpatched loader
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_API_KEY", SENTINEL_KEY)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    code = script.main(
        ["--out", str(tmp_path / "out"), consent, "--case", "kpis_may_occupancy"],
        today=lambda: TODAY,
    )

    printed = capsys.readouterr()
    assert code == 2
    assert world.built == []
    assert IMPLICIT_PROVIDER_REFUSAL in printed.err
    assert "LLM_ENABLED must be true and LLM_API_KEY set." not in printed.err
    assert SENTINEL_KEY not in printed.err + printed.out
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("mode", ["provider_default", "disabled"])
def test_the_effective_thinking_setting_is_recorded_in_both_files(world: World, mode: str) -> None:
    world.thinking = mode
    assert world.run("--case", "kpis_may_occupancy") == 0

    replays, report = world.files()
    assert replays["live_provenance"]["thinking"] == mode
    assert report["live_provenance"]["thinking"] == mode


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


# --- R2.7O: the local provider -------------------------------------------------------------------


def local(world: World) -> World:
    world.provider = "ollama"
    world.consent = "--confirm-local-model-call"
    return world


def test_a_local_run_records_the_provider_model_and_every_effective_option(world: World) -> None:
    """The stand-in answers; nothing reaches a model. What matters is what the files record."""
    assert local(world).run("--case", "kpis_may_occupancy") == 0

    replays, report = world.files()
    assert len(world.built) == 1
    assert replays["provider"] == "ollama"
    assert replays["model"] == "qwen3.5:4b"
    for provenance in (replays["live_provenance"], report["live_provenance"]):
        assert provenance["provider"] == "ollama"
        assert provenance["configured_model"] == "qwen3.5:4b"
        assert provenance["thinking"] == "disabled"
        assert (provenance["sdk_distribution"], provenance["sdk_version"]) == ("none", "none")
        assert provenance["ollama_options"] == {
            "base_url": "http://127.0.0.1:11434",
            "num_ctx": 16_384,
            "num_predict": 1024,
            "temperature": 0.0,
            "seed": 7151,
            "presence_penalty": 0,
            "think": False,
        }


def test_a_paid_run_records_no_local_options(world: World) -> None:
    assert world.run("--case", "kpis_may_occupancy") == 0

    _, report = world.files()
    assert report["live_provenance"]["provider"] == "anthropic"
    assert report["live_provenance"]["ollama_options"] is None


def test_the_paid_flag_is_refused_for_the_local_provider_before_a_client_exists(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    local(world).consent = "--confirm-paid-api-call"

    assert world.run("--case", "kpis_may_occupancy") == 2

    assert world.built == []
    assert "--confirm-local-model-call" in capsys.readouterr().err
    assert not world.out.exists()


def test_the_local_flag_is_refused_for_the_paid_provider_before_a_client_exists(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.consent = "--confirm-local-model-call"  # provider stays anthropic

    assert world.run("--case", "kpis_may_occupancy") == 2

    assert world.built == []
    assert "--confirm-paid-api-call" in capsys.readouterr().err


def test_exactly_one_consent_flag_is_accepted() -> None:
    script = load_script()
    with pytest.raises(SystemExit):
        script.parse_arguments(
            ["--out", "x", "--confirm-paid-api-call", "--confirm-local-model-call"]
        )
    assert script.parse_arguments(["--out", "x", "--confirm-local-model-call"])


@pytest.mark.parametrize(
    ("variable", "value", "fixed"),
    [
        ("OLLAMA_BASE_URL", "http://attacker.invalid:11434", "INVALID_OLLAMA_URL_MESSAGE"),
        ("LLM_BASE_URL", "https://attacker.invalid/v1", "LLM_BASE_URL_WITH_OLLAMA_REFUSAL"),
    ],
)
def test_a_local_misconfiguration_is_refused_by_its_fixed_sentence_before_a_client_exists(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    variable: str,
    value: str,
    fixed: str,
) -> None:
    """The script's REAL `load_settings`, from an empty directory so no `.env` is read."""
    import app.core.config as config

    script = load_script()  # unpatched loader
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)  # the default: ollama
    monkeypatch.setenv(variable, value)

    code = script.main(
        ["--out", str(tmp_path / "out"), "--confirm-local-model-call"], today=lambda: TODAY
    )

    printed = capsys.readouterr()
    assert code == 2
    assert world.built == []
    assert getattr(config, fixed) in printed.err
    assert "attacker" not in printed.err + printed.out


# --- R2.7R: the versioned set ---------------------------------------------------------------------


def test_a_v2_run_records_v2_as_the_set_it_ran(world: World) -> None:
    from tests.evaluation.questions_v2 import COPILOT_EVAL_V2

    assert local(world).run("--set", "copilot_eval_v2", "--case", "kpis_may_occupancy") == 0

    replays, report = world.files()
    selection = replays["live_provenance"]["selection"]
    assert replays["question_set"] == "copilot_eval_v2"
    assert selection["full_set_identity"] == "copilot_eval_v2"
    assert selection["full_set_checksum"] == COPILOT_EVAL_V2.checksum
    assert selection["case_ids"] == ["kpis_may_occupancy"]
    assert report["question_set"]["identity"] == "copilot_eval_v2"
    assert "copilot_eval_v2" in report["statement"]
    assert "copilot_eval_v1" not in report["statement"]


def test_the_default_set_is_still_v1() -> None:
    assert load_script().parse_arguments(
        ["--out", "x", "--confirm-local-model-call"]
    ).question_set == ("copilot_eval_v1")
