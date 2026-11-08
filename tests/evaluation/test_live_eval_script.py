"""R2.6B: `scripts/copilot_live_eval.py`, run end to end with NO provider and NO network.

The provider factory is replaced by a stand-in model, sockets are disabled for every test, and
the settings are built in the test with a sentinel key. What is checked is the script's own
behaviour: what it refuses and when (before any client exists), what identity it records for the
cases it ran, which model ids it records, and that the key appears nowhere it writes or prints.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import socket
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.core.config import Settings
from app.llm.base import ChatRequest, ChatResponse, TokenUsage
from app.llm.errors import LlmUnavailableError
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

    def __init__(
        self, returned: str | None, text: str = "Nothing to report.", fail_first: int = 0
    ) -> None:
        self.returned = returned
        self.text = text
        #: R2.8B: the first this-many requests raise `LlmUnavailableError`, as a stalled
        #: provider's would after the boundary's retry.
        self.fail_first = fail_first
        self.calls = 0
        self.requests: list[ChatRequest] = []

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.calls += 1
        self.requests.append(request)
        if self.calls <= self.fail_first:
            raise LlmUnavailableError()
        return ChatResponse(
            text=self.text,
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
        #: What the stand-in answers (R2.7ZQ: a fixed sentence, to exercise the v3 analysis).
        self.text = "Nothing to report."
        #: R2.8B: how many of the stand-in's first requests raise `LlmUnavailableError`.
        self.fail_first = 0
        #: R2.8B: build the REAL guarded provider instead of the stand-in -- only for a test that
        #: has replaced its transport.
        self.real_factory = False
        #: R2.9H: what an `ollama` run records as its local model's identity. A fixed stand-in,
        #: so no test reads this machine's Ollama directory or asks a server for its version.
        self.local_model: dict[str, Any] = {
            "model": "qwen3.5:4b",
            "server_version": {"status": "unavailable", "reason": "test_stand_in"},
            "manifest": {"status": "unavailable", "reason": "test_stand_in"},
        }

    def run(self, *extra: str, **main_options: Any) -> int:
        return int(
            self.script.main(
                ["--out", str(self.out), self.consent, *extra], today=lambda: TODAY, **main_options
            )
        )

    def files(self, suffix: str = "") -> tuple[dict[str, Any], dict[str, Any]]:
        replays = json.loads(
            (self.out / f"replays-{TODAY}{suffix}.json").read_text(encoding="utf-8")
        )
        report = json.loads((self.out / f"report-{TODAY}{suffix}.json").read_text(encoding="utf-8"))
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
    real_build = factory.build_chat_model

    def build(_settings: Settings) -> Any:
        if state.real_factory:
            return real_build(_settings)
        model = StandInModel(state.returned, state.text, state.fail_first)
        state.built.append(model)
        return model

    monkeypatch.setattr(factory, "build_chat_model", build)

    def local_model(settings: Settings) -> dict[str, Any] | None:
        return dict(state.local_model) if settings.llm_provider == "ollama" else None

    monkeypatch.setattr(script, "local_model_provenance", local_model)
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
    from app.services.copilot import PROMPT
    from tests.evaluation.replay import load_replays

    assert world.run("--case", "kpis_may_occupancy") == 0

    with pytest.raises(ValueError, match="another question set"):
        load_replays(world.out / f"replays-{TODAY}.json", COPILOT_EVAL_V1, prompt=PROMPT)


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


# --- R2.7ZQ: the prompt is chosen by name; v2 stays the default ---------------------------------

V3 = "-copilot_answer-v3"


def test_the_default_prompt_is_v2_and_is_what_the_copilot_serves() -> None:
    from app.llm.prompts.registry import COPILOT_ANSWER_V2
    from app.services.copilot import PROMPT

    script = load_script()
    assert script.parse_arguments(["--out", "x", "--confirm-local-model-call"]).prompt == (
        "copilot_answer@v2"
    )
    assert script.DEFAULT_PROMPT == PROMPT.identity == COPILOT_ANSWER_V2.identity
    assert script.resolve_prompt("copilot_answer@v3").identity == "copilot_answer@v3"


def test_a_default_run_renders_and_records_v2_under_its_usual_names(world: World) -> None:
    from app.llm.prompts.registry import COPILOT_ANSWER_V2

    assert world.run("--case", "kpis_may_occupancy") == 0

    replays, report = world.files()  # the names a v2 run has always written
    v2 = {"identity": COPILOT_ANSWER_V2.identity, "checksum": COPILOT_ANSWER_V2.checksum}
    assert (replays["prompt"], replays["prompt_checksum"]) == (v2["identity"], v2["checksum"])
    assert report["prompt"] == replays["live_provenance"]["prompt"] == v2
    assert report["live_provenance"]["prompt"] == v2
    assert "v3_analysis" not in report
    [request] = world.built[0].requests
    assert (request.prompt_id, request.prompt_version) == ("copilot_answer", "v2")
    assert request.messages[0].content == COPILOT_ANSWER_V2.system


def test_a_v3_run_renders_v3_and_records_it_in_every_file(world: World) -> None:
    from app.llm.prompts.registry import COPILOT_ANSWER_V2, COPILOT_ANSWER_V3
    from app.services.copilot import PROMPT

    assert local(world).run("--set", "copilot_eval_v2", "--prompt", "copilot_answer@v3") == 0

    assert sorted(path.name for path in world.out.iterdir()) == [
        f"replays-{TODAY}{V3}.json",
        f"report-{TODAY}{V3}.json",
    ]
    replays, report = world.files(V3)
    v3 = {"identity": COPILOT_ANSWER_V3.identity, "checksum": COPILOT_ANSWER_V3.checksum}
    assert (replays["prompt"], replays["prompt_checksum"]) == (v3["identity"], v3["checksum"])
    assert report["prompt"] == v3
    for provenance in (replays["live_provenance"], report["live_provenance"]):
        assert provenance["prompt"] == v3
        assert provenance["provider"] == "ollama"
        assert provenance["configured_model"] == "qwen3.5:4b"
        assert set(provenance["budget"]) == {"timeout_seconds", "max_output_tokens"}
    assert "copilot_answer@v3" in report["statement"]
    assert report["v3_analysis"]["prompt"] == v3
    requests = [request for model in world.built for request in model.requests]
    assert requests
    for request in requests:
        assert (request.prompt_id, request.prompt_version) == ("copilot_answer", "v3")
        assert request.messages[0].content == COPILOT_ANSWER_V3.system
    assert PROMPT is COPILOT_ANSWER_V2  # the copilot's own default was never touched


@pytest.mark.parametrize(
    # R2.8E made v4 an allowed choice, R2.9L v5, R2.9O v6, R2.9R v7 and R2.9U v8 (below); v9,
    # which no stage has registered, stands for an unknown version instead.
    "prompt",
    [
        "copilot_answer@v1",
        "copilot_answer@v9",
        "v3",
        "v4",
        "v5",
        "v6",
        "v7",
        "v8",
        "boundary_probe@v1",
    ],
)
def test_an_unknown_prompt_is_refused_by_the_command_line(prompt: str) -> None:
    with pytest.raises(SystemExit):
        load_script().parse_arguments(
            ["--out", "x", "--confirm-local-model-call", "--prompt", prompt]
        )


def test_a_prompt_outside_the_allowed_ones_is_never_resolved() -> None:
    script = load_script()
    for identity in (
        "copilot_answer@v1",
        "copilot_conversation@v2",
        "copilot_answer@v9",
        "copilot_conversation@v1",
        "boundary_probe@v1",
    ):
        with pytest.raises(script.RunRefusedError):
            script.resolve_prompt(identity)


def test_the_unknown_example_is_registered_nowhere() -> None:
    """v9 is a genuinely unknown version (R2.9U registered v8, the previous example): if a later
    stage registers it, these tests need a new example rather than silently testing a known
    prompt."""
    from app.llm.prompts.registry import REGISTRY

    assert "copilot_answer@v9" not in REGISTRY


# --- R2.8E: copilot_answer@v4, an evaluation-only choice -----------------------------------------

V4 = "-copilot_answer-v4"


def test_v4_is_allowed_only_when_named_and_the_default_stays_v2() -> None:
    from app.llm.prompts.registry import COPILOT_ANSWER_V4

    script = load_script()
    assert script.PROMPTS == (
        "copilot_answer@v2",
        "copilot_answer@v3",
        "copilot_answer@v4",
        "copilot_answer@v5",
        "copilot_answer@v6",
        "copilot_answer@v7",
        "copilot_answer@v8",
    )
    assert script.DEFAULT_PROMPT == "copilot_answer@v2"
    unnamed = script.parse_arguments(["--out", "x", "--confirm-local-model-call"])
    assert unnamed.prompt == "copilot_answer@v2"
    named = script.parse_arguments(
        ["--out", "x", "--confirm-local-model-call", "--prompt", "copilot_answer@v4"]
    )
    assert named.prompt == "copilot_answer@v4"
    assert script.resolve_prompt("copilot_answer@v4") is COPILOT_ANSWER_V4


def test_allowing_v4_here_serves_it_nowhere() -> None:
    from app.llm.prompts.registry import COPILOT_ANSWER_V2
    from app.services.copilot import PROMPT

    load_script()
    assert PROMPT is COPILOT_ANSWER_V2


def test_a_v4_run_renders_v4_and_records_it_in_every_file(world: World) -> None:
    from app.llm.prompts.registry import COPILOT_ANSWER_V2, COPILOT_ANSWER_V4, SCOPE_ANSWER
    from app.services.copilot import PROMPT

    local(world).text = SCOPE_ANSWER
    argv = ["--set", "copilot_eval_v3", "--prompt", "copilot_answer@v4"]
    assert world.run(*argv, "--case", "another_property", "--case", "scope_named_competitor") == 0

    assert sorted(path.name for path in world.out.iterdir()) == [
        f"replays-{TODAY}{V4}.json",
        f"report-{TODAY}{V4}.json",
    ]
    replays, report = world.files(V4)
    v4 = {"identity": COPILOT_ANSWER_V4.identity, "checksum": COPILOT_ANSWER_V4.checksum}
    assert (replays["prompt"], replays["prompt_checksum"]) == (v4["identity"], v4["checksum"])
    assert report["prompt"] == v4
    for provenance in (replays["live_provenance"], report["live_provenance"]):
        assert provenance["prompt"] == v4
    assert "copilot_answer@v4" in report["statement"]
    # Every request the model received rendered v4, exactly.
    requests = [request for model in world.built for request in model.requests]
    assert len(requests) == 1  # the server-guard case never reached it
    for request in requests:
        assert (request.prompt_id, request.prompt_version) == ("copilot_answer", "v4")
        assert request.messages[0].content == COPILOT_ANSWER_V4.system
    # The blocks a v4 report carries -- and not the v3-only wording analysis.
    assert {"served", "model_level", "run_health"} <= set(report)
    assert "v3_analysis" not in report
    served = {row["case_id"]: row["answer_source"] for row in report["served"]["cases"]}
    assert served == {"another_property": "scope_guard", "scope_named_competitor": "model"}
    assert PROMPT is COPILOT_ANSWER_V2  # the copilot's own default was never touched


def test_the_v3_analysis_refuses_v4_so_a_v4_run_never_asks_for_it() -> None:
    """Why the script adds `v3_analysis` for v3 only: asked under v4 it refuses, and in a live run
    that refusal would come after every model call and before either file was written."""
    from app.llm.prompts.registry import COPILOT_ANSWER_V4
    from tests.evaluation import v3_pilot_analysis

    with pytest.raises(ValueError, match="copilot_answer@v3's"):
        v3_pilot_analysis.analyse(COPILOT_ANSWER_V4, [], {}, {})
    assert load_script().V3_ANALYSIS_PROMPT == "copilot_answer@v3"


def test_a_v4_run_without_any_confirmation_is_refused_before_a_client_exists(
    world: World,
) -> None:
    local(world)
    with pytest.raises(SystemExit) as refused:
        world.script.main(["--out", str(world.out), "--prompt", "copilot_answer@v4"])
    assert refused.value.code == 2
    assert world.built == []
    assert not world.out.exists()


def test_a_v4_run_with_the_wrong_confirmation_is_refused_before_a_client_exists(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    local(world).consent = "--confirm-paid-api-call"
    assert world.run("--prompt", "copilot_answer@v4") == 2
    assert world.built == []
    assert not world.out.exists()
    assert "--confirm-local-model-call" in capsys.readouterr().err


def test_a_v4_run_refuses_a_directory_that_already_holds_anything(world: World) -> None:
    world.out.mkdir(parents=True)
    (world.out / "notes.txt").write_text("earlier", encoding="utf-8")
    assert local(world).run("--prompt", "copilot_answer@v4") == 2
    assert world.built == []


def test_a_prompt_missing_from_the_registry_is_refused_not_replaced(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.llm.prompts import registry

    monkeypatch.delitem(registry.REGISTRY, "copilot_answer@v3")

    assert local(world).run("--prompt", "copilot_answer@v3") == 2
    assert world.built == []
    assert not world.out.exists()
    assert "copilot_answer@v3" in capsys.readouterr().err


# --- R2.9L: copilot_answer@v5, an evaluation-only choice on v4's terms ----------------------------

V5 = "-copilot_answer-v5"
V5_ARGS = ("--set", "copilot_eval_v3", "--prompt", "copilot_answer@v5")


def test_v5_is_allowed_only_when_named_and_the_default_stays_v2() -> None:
    from app.llm.prompts.registry import COPILOT_ANSWER_V2, COPILOT_ANSWER_V5
    from app.services.copilot import PROMPT

    script = load_script()
    assert "copilot_answer@v5" in script.PROMPTS
    assert script.parse_arguments(["--out", "x", "--confirm-local-model-call"]).prompt == (
        "copilot_answer@v2"
    )
    named = script.parse_arguments(
        ["--out", "x", "--confirm-local-model-call", "--prompt", "copilot_answer@v5"]
    )
    assert named.prompt == "copilot_answer@v5"
    assert script.resolve_prompt("copilot_answer@v5") is COPILOT_ANSWER_V5
    assert PROMPT is COPILOT_ANSWER_V2


def test_a_v5_run_renders_v5_with_the_evaluated_hotels_name_and_records_it(world: World) -> None:
    from app.llm.prompts.registry import (
        COPILOT_ANSWER_V2,
        COPILOT_ANSWER_V4,
        COPILOT_ANSWER_V5,
        SCOPE_ANSWER,
    )
    from app.services.copilot import PROMPT
    from tests.evaluation.harness import EvalHotel
    from tests.evaluation.questions_v3 import COPILOT_EVAL_V3

    local(world).text = SCOPE_ANSWER
    cases = ("--case", "another_property", "--case", "scope_named_competitor")
    assert world.run(*V5_ARGS, *cases) == 0

    assert sorted(path.name for path in world.out.iterdir()) == [
        f"replays-{TODAY}{V5}.json",
        f"report-{TODAY}{V5}.json",
    ]
    replays, report = world.files(V5)
    v5 = {"identity": COPILOT_ANSWER_V5.identity, "checksum": COPILOT_ANSWER_V5.checksum}
    assert (replays["prompt"], replays["prompt_checksum"]) == (v5["identity"], v5["checksum"])
    assert report["prompt"] == v5
    for provenance in (replays["live_provenance"], report["live_provenance"]):
        assert provenance["prompt"] == v5
    assert "copilot_answer@v5" in report["statement"]
    # The one request (the server-guard case never reached a model) rendered v5: v4's system
    # turn, and the identity sentence naming the evaluated hotel before the question.
    [question] = [
        case.question for case in COPILOT_EVAL_V3.cases if case.case_id == "scope_named_competitor"
    ]
    [request] = [request for model in world.built for request in model.requests]
    assert (request.prompt_id, request.prompt_version) == ("copilot_answer", "v5")
    assert request.messages[0].content == COPILOT_ANSWER_V4.system
    assert (
        request.messages[-1].content
        == COPILOT_ANSWER_V5.render(question=question, hotel_name=EvalHotel.name)[1].content
    )
    assert request.messages[-1].content.startswith(
        "The hotel this request is for is named Evaluation Fixture Hotel;"
    )
    assert {"served", "model_level", "run_health", "model_level_v2", "integrity_v3"} <= set(report)
    assert "v3_analysis" not in report
    served = {row["case_id"]: row["answer_source"] for row in report["served"]["cases"]}
    assert served == {"another_property": "scope_guard", "scope_named_competitor": "model"}
    assert PROMPT is COPILOT_ANSWER_V2


def test_v5_file_names_are_its_own_and_carry_the_attempt_of_a_repeated_run() -> None:
    script = load_script()
    out = Path("anywhere")
    assert [p.name for p in script.output_paths(out, "2026-11-02", "copilot_answer@v5")] == [
        "replays-2026-11-02-copilot_answer-v5.json",
        "report-2026-11-02-copilot_answer-v5.json",
    ]
    assert [p.name for p in script.output_paths(out, "2026-11-02", "copilot_answer@v5", 2)] == [
        "replays-2026-11-02-copilot_answer-v5-attempt-2.json",
        "report-2026-11-02-copilot_answer-v5-attempt-2.json",
    ]
    assert script.repeats_path(out, "2026-11-02", "copilot_answer@v5").name == (
        "repeats-2026-11-02-copilot_answer-v5.json"
    )
    assert script.attempt_id("2026-11-02", "copilot_answer@v5", 3, 3) == (
        "2026-11-02-copilot_answer-v5-attempt-3-of-3"
    )


def test_a_v5_run_without_any_confirmation_is_refused_before_a_client_exists(
    world: World,
) -> None:
    local(world)
    with pytest.raises(SystemExit) as refused:
        world.script.main(["--out", str(world.out), "--prompt", "copilot_answer@v5"])
    assert refused.value.code == 2
    assert world.built == []
    assert not world.out.exists()


def test_a_v5_run_with_the_wrong_confirmation_is_refused_before_a_client_exists(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    local(world).consent = "--confirm-paid-api-call"
    assert world.run("--prompt", "copilot_answer@v5") == 2
    assert world.built == []
    assert not world.out.exists()
    assert "--confirm-local-model-call" in capsys.readouterr().err


def test_a_v5_run_refuses_a_directory_that_already_holds_anything(world: World) -> None:
    world.out.mkdir(parents=True)
    (world.out / "notes.txt").write_text("earlier", encoding="utf-8")
    assert local(world).run("--prompt", "copilot_answer@v5") == 2
    assert world.built == []
    assert [path.name for path in world.out.iterdir()] == ["notes.txt"]


def test_a_v5_run_is_never_repeated_into_its_own_directory(world: World) -> None:
    assert local(world).run(*V5_ARGS, "--case", "guest_names") == 0
    before = {path.name: path.read_bytes() for path in world.out.iterdir()}
    built = len(world.built)

    assert world.run(*V5_ARGS, "--case", "guest_names") == 2
    assert len(world.built) == built  # refused before a client existed
    assert {path.name: path.read_bytes() for path in world.out.iterdir()} == before


def test_a_v5_run_inside_the_repository_is_refused(world: World) -> None:
    for inside in (REPO_ROOT / "tests" / "evaluation", REPO_ROOT / "v5-pilot-must-not-exist"):
        world.out = inside
        assert local(world).run("--prompt", "copilot_answer@v5") == 2
    assert world.built == []
    assert not (REPO_ROOT / "v5-pilot-must-not-exist").exists()


def test_a_v5_run_with_an_invalid_repeat_is_refused_before_a_client_exists(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as refused:
        local(world).run("--prompt", "copilot_answer@v5", "--repeat", "11")
    assert refused.value.code == 2
    assert "--repeat must be a whole number from 1 to 10" in capsys.readouterr().err
    assert world.built == []
    assert not world.out.exists()


def test_v5_missing_from_the_registry_is_refused_not_replaced(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.llm.prompts import registry

    monkeypatch.delitem(registry.REGISTRY, "copilot_answer@v5")

    assert local(world).run("--prompt", "copilot_answer@v5") == 2
    assert world.built == []
    assert not world.out.exists()
    assert "copilot_answer@v5" in capsys.readouterr().err


# --- R2.9O: copilot_answer@v6, on v5's terms ------------------------------------------------------

V6 = "-copilot_answer-v6"
V6_ARGS = ("--set", "copilot_eval_v3", "--prompt", "copilot_answer@v6")


def test_v6_is_allowed_only_when_named_and_the_default_stays_v2() -> None:
    from app.llm.prompts.registry import COPILOT_ANSWER_V2, COPILOT_ANSWER_V6
    from app.services.copilot import PROMPT

    script = load_script()
    assert "copilot_answer@v6" in script.PROMPTS
    assert script.DEFAULT_PROMPT == "copilot_answer@v2"
    assert script.parse_arguments(["--out", "x", "--confirm-local-model-call"]).prompt == (
        "copilot_answer@v2"
    )
    named = script.parse_arguments(
        ["--out", "x", "--confirm-local-model-call", "--prompt", "copilot_answer@v6"]
    )
    assert named.prompt == "copilot_answer@v6"
    assert script.resolve_prompt("copilot_answer@v6") is COPILOT_ANSWER_V6
    assert PROMPT is COPILOT_ANSWER_V2


def test_a_v6_run_renders_v6_with_the_evaluated_hotels_name_and_records_it(world: World) -> None:
    from app.llm.prompts.registry import (
        COPILOT_ANSWER_V2,
        COPILOT_ANSWER_V4,
        COPILOT_ANSWER_V6,
        SCOPE_ANSWER,
    )
    from app.services.copilot import PROMPT
    from tests.evaluation.harness import EvalHotel
    from tests.evaluation.questions_v3 import COPILOT_EVAL_V3

    local(world).text = SCOPE_ANSWER
    cases = ("--case", "another_property", "--case", "scope_place_across_the_road")
    assert world.run(*V6_ARGS, *cases) == 0

    assert sorted(path.name for path in world.out.iterdir()) == [
        f"replays-{TODAY}{V6}.json",
        f"report-{TODAY}{V6}.json",
    ]
    replays, report = world.files(V6)
    v6 = {"identity": COPILOT_ANSWER_V6.identity, "checksum": COPILOT_ANSWER_V6.checksum}
    assert (replays["prompt"], replays["prompt_checksum"]) == (v6["identity"], v6["checksum"])
    assert report["prompt"] == v6
    for provenance in (replays["live_provenance"], report["live_provenance"]):
        assert provenance["prompt"] == v6
        assert provenance["provider"] == "ollama"
    assert "copilot_answer@v6" in report["statement"]
    [question] = [
        case.question
        for case in COPILOT_EVAL_V3.cases
        if case.case_id == "scope_place_across_the_road"
    ]
    [request] = [request for model in world.built for request in model.requests]
    assert (request.prompt_id, request.prompt_version) == ("copilot_answer", "v6")
    assert request.messages[0].content == COPILOT_ANSWER_V4.system
    assert (
        request.messages[-1].content
        == COPILOT_ANSWER_V6.render(question=question, hotel_name=EvalHotel.name)[1].content
    )
    assert request.messages[-1].content.startswith(
        "The hotel this request is for is Evaluation Fixture Hotel. Questions about a different"
    )
    assert request.messages[-1].content.endswith(f"\n\n{question}")
    assert {"served", "model_level", "run_health", "model_level_v2", "integrity_v3"} <= set(report)
    assert "v3_analysis" not in report
    served = {row["case_id"]: row["answer_source"] for row in report["served"]["cases"]}
    assert served == {"another_property": "scope_guard", "scope_place_across_the_road": "model"}
    assert PROMPT is COPILOT_ANSWER_V2


def test_v6_file_names_are_its_own_and_carry_the_attempt_of_a_repeated_run() -> None:
    script = load_script()
    out = Path("anywhere")
    assert [p.name for p in script.output_paths(out, "2026-11-03", "copilot_answer@v6")] == [
        "replays-2026-11-03-copilot_answer-v6.json",
        "report-2026-11-03-copilot_answer-v6.json",
    ]
    assert [p.name for p in script.output_paths(out, "2026-11-03", "copilot_answer@v6", 3)] == [
        "replays-2026-11-03-copilot_answer-v6-attempt-3.json",
        "report-2026-11-03-copilot_answer-v6-attempt-3.json",
    ]
    assert script.repeats_path(out, "2026-11-03", "copilot_answer@v6").name == (
        "repeats-2026-11-03-copilot_answer-v6.json"
    )
    assert script.attempt_id("2026-11-03", "copilot_answer@v6", 1, 3) == (
        "2026-11-03-copilot_answer-v6-attempt-1-of-3"
    )


def test_a_v6_run_without_any_confirmation_is_refused_before_a_client_exists(
    world: World,
) -> None:
    local(world)
    with pytest.raises(SystemExit) as refused:
        world.script.main(["--out", str(world.out), "--prompt", "copilot_answer@v6"])
    assert refused.value.code == 2
    assert world.built == []
    assert not world.out.exists()


def test_a_v6_run_with_the_wrong_confirmation_is_refused_before_a_client_exists(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    local(world).consent = "--confirm-paid-api-call"
    assert world.run("--prompt", "copilot_answer@v6") == 2
    assert world.built == []
    assert not world.out.exists()
    assert "--confirm-local-model-call" in capsys.readouterr().err


def test_a_v6_run_refuses_a_directory_that_already_holds_anything(world: World) -> None:
    world.out.mkdir(parents=True)
    (world.out / "notes.txt").write_text("earlier", encoding="utf-8")
    assert local(world).run("--prompt", "copilot_answer@v6") == 2
    assert world.built == []
    assert [path.name for path in world.out.iterdir()] == ["notes.txt"]


def test_a_v6_run_accepts_a_new_empty_directory(world: World) -> None:
    world.out.mkdir()
    assert local(world).run(*V6_ARGS, "--case", "guest_names") == 0
    assert sorted(path.name for path in world.out.iterdir()) == [
        f"replays-{TODAY}{V6}.json",
        f"report-{TODAY}{V6}.json",
    ]


def test_a_v6_run_is_never_repeated_into_its_own_directory(world: World) -> None:
    assert local(world).run(*V6_ARGS, "--case", "guest_names") == 0
    before = {path.name: path.read_bytes() for path in world.out.iterdir()}
    built = len(world.built)

    assert world.run(*V6_ARGS, "--case", "guest_names") == 2
    assert len(world.built) == built  # refused before a client existed
    assert {path.name: path.read_bytes() for path in world.out.iterdir()} == before


def test_a_v6_run_inside_the_repository_is_refused(world: World) -> None:
    for inside in (REPO_ROOT / "tests" / "evaluation", REPO_ROOT / "v6-pilot-must-not-exist"):
        world.out = inside
        assert local(world).run("--prompt", "copilot_answer@v6") == 2
    assert world.built == []
    assert not (REPO_ROOT / "v6-pilot-must-not-exist").exists()


@pytest.mark.parametrize("value", ["0", "11", "two"])
def test_a_v6_run_with_an_invalid_repeat_is_refused_before_a_client_exists(
    world: World, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    with pytest.raises(SystemExit) as refused:
        local(world).run("--prompt", "copilot_answer@v6", "--repeat", value)
    assert refused.value.code == 2
    assert "--repeat must be a whole number from 1 to 10" in capsys.readouterr().err
    assert world.built == []
    assert not world.out.exists()


def test_v6_missing_from_the_registry_is_refused_not_replaced(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.llm.prompts import registry

    monkeypatch.delitem(registry.REGISTRY, "copilot_answer@v6")

    assert local(world).run("--prompt", "copilot_answer@v6") == 2
    assert world.built == []
    assert not world.out.exists()
    assert "copilot_answer@v6" in capsys.readouterr().err


# --- R2.9R: copilot_answer@v7, on v6's terms ------------------------------------------------------

V7 = "-copilot_answer-v7"
V7_ARGS = ("--set", "copilot_eval_v3", "--prompt", "copilot_answer@v7")


def test_v7_is_allowed_only_when_named_and_the_default_stays_v2() -> None:
    from app.llm.prompts.registry import COPILOT_ANSWER_V2, COPILOT_ANSWER_V7
    from app.services.copilot import PROMPT

    script = load_script()
    assert "copilot_answer@v7" in script.PROMPTS
    assert script.DEFAULT_PROMPT == "copilot_answer@v2"
    assert script.parse_arguments(["--out", "x", "--confirm-local-model-call"]).prompt == (
        "copilot_answer@v2"
    )
    named = script.parse_arguments(
        ["--out", "x", "--confirm-local-model-call", "--prompt", "copilot_answer@v7"]
    )
    assert named.prompt == "copilot_answer@v7"
    assert script.resolve_prompt("copilot_answer@v7") is COPILOT_ANSWER_V7
    assert PROMPT is COPILOT_ANSWER_V2


def test_a_v7_run_renders_v7_with_the_evaluated_hotels_name_and_records_it(world: World) -> None:
    from app.llm.prompts.registry import (
        COPILOT_ANSWER_V2,
        COPILOT_ANSWER_V4,
        COPILOT_ANSWER_V7,
        PERSONAL_DATA_ANSWER,
    )
    from app.services.copilot import PROMPT
    from tests.evaluation.harness import EvalHotel
    from tests.evaluation.questions_v3 import COPILOT_EVAL_V3

    local(world).text = PERSONAL_DATA_ANSWER
    assert world.run(*V7_ARGS, "--case", "another_property", "--case", "guest_names") == 0

    assert sorted(path.name for path in world.out.iterdir()) == [
        f"replays-{TODAY}{V7}.json",
        f"report-{TODAY}{V7}.json",
    ]
    replays, report = world.files(V7)
    v7 = {"identity": COPILOT_ANSWER_V7.identity, "checksum": COPILOT_ANSWER_V7.checksum}
    assert (replays["prompt"], replays["prompt_checksum"]) == (v7["identity"], v7["checksum"])
    assert report["prompt"] == v7
    for provenance in (replays["live_provenance"], report["live_provenance"]):
        assert provenance["prompt"] == v7
        assert provenance["provider"] == "ollama"
    assert "copilot_answer@v7" in report["statement"]
    [question] = [case.question for case in COPILOT_EVAL_V3.cases if case.case_id == "guest_names"]
    [request] = [request for model in world.built for request in model.requests]
    assert (request.prompt_id, request.prompt_version) == ("copilot_answer", "v7")
    assert request.messages[0].content == COPILOT_ANSWER_V4.system
    assert (
        request.messages[-1].content
        == COPILOT_ANSWER_V7.render(question=question, hotel_name=EvalHotel.name)[1].content
    )
    assert request.messages[-1].content.startswith(
        "The hotel this request is for is Evaluation Fixture Hotel. Questions about a different"
    )
    assert "A question about which guests or who stayed" in request.messages[-1].content
    assert request.messages[-1].content.endswith(f"\n\n{question}")
    assert {"served", "model_level", "run_health", "model_level_v2", "integrity_v3"} <= set(report)
    assert "v3_analysis" not in report
    served = {row["case_id"]: row["answer_source"] for row in report["served"]["cases"]}
    assert served == {"another_property": "scope_guard", "guest_names": "model"}
    assert PROMPT is COPILOT_ANSWER_V2


def test_v7_file_names_are_its_own_and_carry_the_attempt_of_a_repeated_run() -> None:
    script = load_script()
    out = Path("anywhere")
    assert [p.name for p in script.output_paths(out, "2026-11-03", "copilot_answer@v7")] == [
        "replays-2026-11-03-copilot_answer-v7.json",
        "report-2026-11-03-copilot_answer-v7.json",
    ]
    assert [p.name for p in script.output_paths(out, "2026-11-03", "copilot_answer@v7", 2)] == [
        "replays-2026-11-03-copilot_answer-v7-attempt-2.json",
        "report-2026-11-03-copilot_answer-v7-attempt-2.json",
    ]
    assert script.repeats_path(out, "2026-11-03", "copilot_answer@v7").name == (
        "repeats-2026-11-03-copilot_answer-v7.json"
    )
    assert script.attempt_id("2026-11-03", "copilot_answer@v7", 2, 3) == (
        "2026-11-03-copilot_answer-v7-attempt-2-of-3"
    )


def test_a_v7_run_without_any_confirmation_is_refused_before_a_client_exists(
    world: World,
) -> None:
    local(world)
    with pytest.raises(SystemExit) as refused:
        world.script.main(["--out", str(world.out), "--prompt", "copilot_answer@v7"])
    assert refused.value.code == 2
    assert world.built == []
    assert not world.out.exists()


def test_a_v7_run_with_the_wrong_confirmation_is_refused_before_a_client_exists(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    local(world).consent = "--confirm-paid-api-call"
    assert world.run("--prompt", "copilot_answer@v7") == 2
    assert world.built == []
    assert not world.out.exists()
    assert "--confirm-local-model-call" in capsys.readouterr().err


def test_a_v7_run_refuses_a_directory_that_already_holds_anything(world: World) -> None:
    world.out.mkdir(parents=True)
    (world.out / "notes.txt").write_text("earlier", encoding="utf-8")
    assert local(world).run("--prompt", "copilot_answer@v7") == 2
    assert world.built == []
    assert [path.name for path in world.out.iterdir()] == ["notes.txt"]


def test_a_v7_run_accepts_a_new_empty_directory(world: World) -> None:
    world.out.mkdir()
    assert local(world).run(*V7_ARGS, "--case", "guest_names") == 0
    assert sorted(path.name for path in world.out.iterdir()) == [
        f"replays-{TODAY}{V7}.json",
        f"report-{TODAY}{V7}.json",
    ]


def test_a_v7_run_is_never_repeated_into_its_own_directory(world: World) -> None:
    assert local(world).run(*V7_ARGS, "--case", "guest_names") == 0
    before = {path.name: path.read_bytes() for path in world.out.iterdir()}
    built = len(world.built)

    assert world.run(*V7_ARGS, "--case", "guest_names") == 2
    assert len(world.built) == built  # refused before a client existed
    assert {path.name: path.read_bytes() for path in world.out.iterdir()} == before


def test_a_v7_run_inside_the_repository_is_refused(world: World) -> None:
    for inside in (REPO_ROOT / "tests" / "evaluation", REPO_ROOT / "v7-pilot-must-not-exist"):
        world.out = inside
        assert local(world).run("--prompt", "copilot_answer@v7") == 2
    assert world.built == []
    assert not (REPO_ROOT / "v7-pilot-must-not-exist").exists()


@pytest.mark.parametrize("value", ["0", "11", "two"])
def test_a_v7_run_with_an_invalid_repeat_is_refused_before_a_client_exists(
    world: World, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    with pytest.raises(SystemExit) as refused:
        local(world).run("--prompt", "copilot_answer@v7", "--repeat", value)
    assert refused.value.code == 2
    assert "--repeat must be a whole number from 1 to 10" in capsys.readouterr().err
    assert world.built == []
    assert not world.out.exists()


def test_v7_missing_from_the_registry_is_refused_not_replaced(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.llm.prompts import registry

    monkeypatch.delitem(registry.REGISTRY, "copilot_answer@v7")

    assert local(world).run("--prompt", "copilot_answer@v7") == 2
    assert world.built == []
    assert not world.out.exists()
    assert "copilot_answer@v7" in capsys.readouterr().err


# --- R2.9U: copilot_answer@v8, on v7's terms ------------------------------------------------------

V8 = "-copilot_answer-v8"
V8_ARGS = ("--set", "copilot_eval_v3", "--prompt", "copilot_answer@v8")


def test_v8_is_allowed_only_when_named_and_the_default_stays_v2() -> None:
    from app.llm.prompts.registry import COPILOT_ANSWER_V2, COPILOT_ANSWER_V8
    from app.services.copilot import PROMPT

    script = load_script()
    assert "copilot_answer@v8" in script.PROMPTS
    assert script.DEFAULT_PROMPT == "copilot_answer@v2"
    assert script.parse_arguments(["--out", "x", "--confirm-local-model-call"]).prompt == (
        "copilot_answer@v2"
    )
    named = script.parse_arguments(
        ["--out", "x", "--confirm-local-model-call", "--prompt", "copilot_answer@v8"]
    )
    assert named.prompt == "copilot_answer@v8"
    assert script.resolve_prompt("copilot_answer@v8") is COPILOT_ANSWER_V8
    assert PROMPT is COPILOT_ANSWER_V2


def test_a_v8_run_renders_v8_with_the_evaluated_hotels_name_and_records_it(world: World) -> None:
    from app.llm.prompts.registry import (
        COPILOT_ANSWER_V2,
        COPILOT_ANSWER_V4,
        COPILOT_ANSWER_V8,
        PERSONAL_DATA_ANSWER,
    )
    from app.services.copilot import PROMPT
    from tests.evaluation.harness import EvalHotel
    from tests.evaluation.questions_v3 import COPILOT_EVAL_V3

    local(world).text = PERSONAL_DATA_ANSWER
    assert world.run(*V8_ARGS, "--case", "another_property", "--case", "guest_names") == 0

    assert sorted(path.name for path in world.out.iterdir()) == [
        f"replays-{TODAY}{V8}.json",
        f"report-{TODAY}{V8}.json",
    ]
    replays, report = world.files(V8)
    v8 = {"identity": COPILOT_ANSWER_V8.identity, "checksum": COPILOT_ANSWER_V8.checksum}
    assert (replays["prompt"], replays["prompt_checksum"]) == (v8["identity"], v8["checksum"])
    assert report["prompt"] == v8
    for provenance in (replays["live_provenance"], report["live_provenance"]):
        assert provenance["prompt"] == v8
        assert provenance["provider"] == "ollama"
    assert "copilot_answer@v8" in report["statement"]
    [question] = [case.question for case in COPILOT_EVAL_V3.cases if case.case_id == "guest_names"]
    [request] = [request for model in world.built for request in model.requests]
    assert (request.prompt_id, request.prompt_version) == ("copilot_answer", "v8")
    assert request.messages[0].content == COPILOT_ANSWER_V4.system
    assert (
        request.messages[-1].content
        == COPILOT_ANSWER_V8.render(question=question, hotel_name=EvalHotel.name)[1].content
    )
    assert request.messages[-1].content.startswith(
        "The hotel this request is for is Evaluation Fixture Hotel. Questions about a different"
    )
    assert "A question about which guests or who stayed" in request.messages[-1].content
    assert "A question about what guests wrote in their reviews" in request.messages[-1].content
    assert request.messages[-1].content.endswith(f"\n\n{question}")
    assert {"served", "model_level", "run_health", "model_level_v2", "integrity_v3"} <= set(report)
    assert "v3_analysis" not in report
    served = {row["case_id"]: row["answer_source"] for row in report["served"]["cases"]}
    assert served == {"another_property": "scope_guard", "guest_names": "model"}
    assert PROMPT is COPILOT_ANSWER_V2


def test_v8_file_names_are_its_own_and_carry_the_attempt_of_a_repeated_run() -> None:
    script = load_script()
    out = Path("anywhere")
    assert [p.name for p in script.output_paths(out, "2026-11-03", "copilot_answer@v8")] == [
        "replays-2026-11-03-copilot_answer-v8.json",
        "report-2026-11-03-copilot_answer-v8.json",
    ]
    assert [p.name for p in script.output_paths(out, "2026-11-03", "copilot_answer@v8", 2)] == [
        "replays-2026-11-03-copilot_answer-v8-attempt-2.json",
        "report-2026-11-03-copilot_answer-v8-attempt-2.json",
    ]
    assert script.repeats_path(out, "2026-11-03", "copilot_answer@v8").name == (
        "repeats-2026-11-03-copilot_answer-v8.json"
    )
    assert script.attempt_id("2026-11-03", "copilot_answer@v8", 2, 3) == (
        "2026-11-03-copilot_answer-v8-attempt-2-of-3"
    )


def test_a_v8_run_without_any_confirmation_is_refused_before_a_client_exists(
    world: World,
) -> None:
    local(world)
    with pytest.raises(SystemExit) as refused:
        world.script.main(["--out", str(world.out), "--prompt", "copilot_answer@v8"])
    assert refused.value.code == 2
    assert world.built == []
    assert not world.out.exists()


def test_a_v8_run_with_the_wrong_confirmation_is_refused_before_a_client_exists(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    local(world).consent = "--confirm-paid-api-call"
    assert world.run("--prompt", "copilot_answer@v8") == 2
    assert world.built == []
    assert not world.out.exists()
    assert "--confirm-local-model-call" in capsys.readouterr().err


def test_a_v8_run_refuses_a_directory_that_already_holds_anything(world: World) -> None:
    world.out.mkdir(parents=True)
    (world.out / "notes.txt").write_text("earlier", encoding="utf-8")
    assert local(world).run("--prompt", "copilot_answer@v8") == 2
    assert world.built == []
    assert [path.name for path in world.out.iterdir()] == ["notes.txt"]


def test_a_v8_run_accepts_a_new_empty_directory(world: World) -> None:
    world.out.mkdir()
    assert local(world).run(*V8_ARGS, "--case", "guest_names") == 0
    assert sorted(path.name for path in world.out.iterdir()) == [
        f"replays-{TODAY}{V8}.json",
        f"report-{TODAY}{V8}.json",
    ]


def test_a_v8_run_is_never_repeated_into_its_own_directory(world: World) -> None:
    assert local(world).run(*V8_ARGS, "--case", "guest_names") == 0
    before = {path.name: path.read_bytes() for path in world.out.iterdir()}
    built = len(world.built)

    assert world.run(*V8_ARGS, "--case", "guest_names") == 2
    assert len(world.built) == built  # refused before a client existed
    assert {path.name: path.read_bytes() for path in world.out.iterdir()} == before


def test_a_v8_run_inside_the_repository_is_refused(world: World) -> None:
    for inside in (REPO_ROOT / "tests" / "evaluation", REPO_ROOT / "v8-pilot-must-not-exist"):
        world.out = inside
        assert local(world).run("--prompt", "copilot_answer@v8") == 2
    assert world.built == []
    assert not (REPO_ROOT / "v8-pilot-must-not-exist").exists()


@pytest.mark.parametrize("value", ["0", "11", "two"])
def test_a_v8_run_with_an_invalid_repeat_is_refused_before_a_client_exists(
    world: World, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    with pytest.raises(SystemExit) as refused:
        local(world).run("--prompt", "copilot_answer@v8", "--repeat", value)
    assert refused.value.code == 2
    assert "--repeat must be a whole number from 1 to 10" in capsys.readouterr().err
    assert world.built == []
    assert not world.out.exists()


def test_v8_missing_from_the_registry_is_refused_not_replaced(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.llm.prompts import registry

    monkeypatch.delitem(registry.REGISTRY, "copilot_answer@v8")

    assert local(world).run("--prompt", "copilot_answer@v8") == 2
    assert world.built == []
    assert not world.out.exists()
    assert "copilot_answer@v8" in capsys.readouterr().err


# --- R2.7ZQ: a v3 run's files are its own ---------------------------------------------------------


def test_v3_file_names_never_collide_with_a_v2_runs() -> None:
    script = load_script()
    out = Path("anywhere")
    v2 = script.output_paths(out, "2026-10-27")
    v3 = script.output_paths(out, "2026-10-27", "copilot_answer@v3")

    assert [p.name for p in v2] == ["replays-2026-10-27.json", "report-2026-10-27.json"]
    assert [p.name for p in v3] == [
        "replays-2026-10-27-copilot_answer-v3.json",
        "report-2026-10-27-copilot_answer-v3.json",
    ]
    assert script.output_paths(out, "2026-10-27", "copilot_answer@v2") == v2


def test_a_v3_run_refuses_a_directory_that_already_holds_anything(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert local(world).run("--case", "kpis_may_occupancy") == 0  # a v2 run, in that directory
    before = {path.name: path.read_bytes() for path in world.out.iterdir()}
    built = len(world.built)

    assert world.run("--prompt", "copilot_answer@v3", "--case", "kpis_may_occupancy") == 2

    assert len(world.built) == built  # refused before a client existed
    assert {path.name: path.read_bytes() for path in world.out.iterdir()} == before
    assert "not empty" in capsys.readouterr().err


def test_a_v3_run_is_never_repeated_into_its_own_directory(world: World) -> None:
    assert local(world).run("--prompt", "copilot_answer@v3", "--case", "guest_names") == 0
    before = {path.name: path.read_bytes() for path in world.out.iterdir()}

    assert world.run("--prompt", "copilot_answer@v3", "--case", "guest_names") == 2
    assert {path.name: path.read_bytes() for path in world.out.iterdir()} == before


def test_a_v3_run_accepts_a_new_empty_directory(world: World) -> None:
    world.out.mkdir()
    assert local(world).run("--prompt", "copilot_answer@v3", "--case", "guest_names") == 0
    assert len(list(world.out.iterdir())) == 2


def test_a_v3_run_refuses_a_file_where_its_directory_should_be(world: World) -> None:
    world.out.write_text("not a directory", encoding="utf-8")
    assert local(world).run("--prompt", "copilot_answer@v3") == 2
    assert world.built == []


def test_a_v3_run_inside_the_repository_is_refused(world: World) -> None:
    for inside in (REPO_ROOT / "tests" / "evaluation", REPO_ROOT / "v3-pilot-must-not-exist"):
        world.out = inside
        assert local(world).run("--prompt", "copilot_answer@v3") == 2
    assert world.built == []
    assert not (REPO_ROOT / "v3-pilot-must-not-exist").exists()


def test_the_key_is_in_no_file_of_a_v3_run(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert world.run("--prompt", "copilot_answer@v3", "--case", "guest_names") == 0

    printed = capsys.readouterr()
    for path in world.out.iterdir():
        assert SENTINEL_KEY not in path.read_text(encoding="utf-8")
    assert SENTINEL_KEY not in printed.out + printed.err


# --- R2.7ZQ: the v3 analysis, end to end ---------------------------------------------------------


def test_a_v3_report_classifies_wording_apart_from_outcome_and_tools(world: World) -> None:
    from app.llm.prompts.registry import PERSONAL_DATA_ANSWER

    local(world).text = PERSONAL_DATA_ANSWER
    argv = ["--set", "copilot_eval_v2", "--prompt", "copilot_answer@v3"]
    assert world.run(*argv, "--case", "guest_names", "--case", "inject_other_hotel") == 0

    _, report = world.files(V3)
    rows = {row["case_id"]: row for row in report["v3_analysis"]["cases"]}
    assert rows["guest_names"]["wording"] == "personal_data"
    assert rows["guest_names"]["outcome"] == "completed"
    assert (rows["guest_names"]["tools_called"], rows["guest_names"]["model_calls"]) == ([], 1)
    # Refused by the server before any model call (R2.7ZL): no model text to classify.
    assert rows["inject_other_hotel"]["wording"] == "no_text"
    assert rows["inject_other_hotel"]["model_calls"] == 0
    # The shared case results carry no wording field: their schema is the frozen reports'.
    assert all("wording" not in case for case in report["cases"])


# --- R2.7ZU: the served side, end to end ----------------------------------------------------------


def test_a_v3_report_tells_the_servers_refusal_from_the_models(world: World) -> None:
    from app.llm.prompts.registry import CANNOT_ANSWER, PERSONAL_DATA_ANSWER
    from app.services.copilot import NOT_FOUND_ANSWER

    local(world).text = PERSONAL_DATA_ANSWER
    argv = ["--set", "copilot_eval_v2", "--prompt", "copilot_answer@v3"]
    assert world.run(*argv, "--case", "guest_names", "--case", "another_property") == 0

    _, report = world.files(V3)
    served = {row["case_id"]: row for row in report["served"]["cases"]}
    analysed = {row["case_id"]: row for row in report["v3_analysis"]["cases"]}
    assert (served["guest_names"]["answer_source"], served["guest_names"]["model_text"]) == (
        "model",
        PERSONAL_DATA_ANSWER,
    )
    assert (
        served["another_property"]["answer_source"],
        served["another_property"]["model_text"],
    ) == (
        "scope_guard",
        None,
    )
    assert (analysed["guest_names"]["wording"], analysed["guest_names"]["served_wording"]) == (
        "personal_data",
        "personal_data",
    )
    assert (
        analysed["another_property"]["wording"],
        analysed["another_property"]["served_wording"],
    ) == (
        "no_text",
        "scope",
    )
    assert analysed["another_property"]["answer_source"] == "scope_guard"
    assert NOT_FOUND_ANSWER != CANNOT_ANSWER  # the two are never confused below
    assert "served" not in report["cases"][0]  # the shared case results are unchanged


def test_a_substituted_answer_is_recorded_as_both_texts(world: World) -> None:
    from app.llm.prompts.registry import CANNOT_ANSWER
    from app.services.copilot import NOT_FOUND_ANSWER

    local(world).text = NOT_FOUND_ANSWER
    assert world.run("--set", "copilot_eval_v2", "--case", "pricing_advice") == 0

    _, report = world.files()
    [row] = report["served"]["cases"]
    assert (row["model_text"], row["served_answer"], row["answer_source"]) == (
        NOT_FOUND_ANSWER,
        CANNOT_ANSWER,
        "cannot_answer_substitution",
    )
    [case] = report["cases"]
    assert case["refusal_correct"] is True and row["served_refusal_correct"] is True


def test_a_knowledge_run_adds_no_served_block(world: World) -> None:
    """Its case results already carry `served_answer`; the knowledge report is unchanged."""
    case = COPILOT_KNOWLEDGE_EVAL_V1.cases[0].case_id
    assert world.run("--set", "copilot_knowledge_eval_v1", "--case", case) == 0

    _, report = world.files()
    assert "served" not in report
    assert "served_answer" in report["cases"][0]


def test_a_knowledge_run_adds_no_model_level_block(world: World) -> None:
    case = COPILOT_KNOWLEDGE_EVAL_V1.cases[0].case_id
    assert world.run("--set", "copilot_knowledge_eval_v1", "--case", case) == 0

    _, report = world.files()
    assert "model_level" not in report


# --- R2.8B: the model-level block, the source checkout, request timing and run health ----------

FIRST_THREE = [case.case_id for case in COPILOT_EVAL_V1.cases[:3]]


def three_cases() -> list[str]:
    return [arg for case in FIRST_THREE for arg in ("--case", case)]


def test_a_copilot_eval_run_carries_the_model_level_block(world: World) -> None:
    from tests.evaluation.model_level import MODEL_LEVEL_VERSION

    assert world.run("--case", "kpis_may_occupancy") == 0

    _, report = world.files()
    assert report["model_level"]["version"] == MODEL_LEVEL_VERSION
    assert report["model_level"]["outcomes"]["final_response"] == 1


class FakeGit:
    """Stands in for `subprocess.run`; answers `rev-parse` and `status`, records every argv."""

    def __init__(self, head: str | Exception, status: str | Exception) -> None:
        self.answers = {"rev-parse": head, "status": status}
        self.argv: list[list[str]] = []

    def __call__(self, argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        self.argv.append(argv)
        assert options["check"] is True and options["capture_output"] is True
        answer = self.answers[argv[3]]
        if isinstance(answer, Exception):
            raise answer
        return subprocess.CompletedProcess(argv, 0, stdout=answer, stderr="")


COMMIT = "aa7ebe0b33adc78a039e357c1ede03da0fdaa37d"


@pytest.mark.parametrize(
    ("head", "status", "expected"),
    [
        (f"{COMMIT}\n", "", {"git_commit": COMMIT, "git_tree": "clean"}),
        (
            f"{COMMIT}\n",
            " M scripts/x.py\n?? new.txt\n",
            {"git_commit": COMMIT, "git_tree": "dirty"},
        ),
        (
            FileNotFoundError("git"),
            FileNotFoundError("git"),
            {"git_commit": "UNKNOWN", "git_tree": "UNKNOWN"},
        ),
        (
            subprocess.CalledProcessError(128, "git"),
            "",
            {"git_commit": "UNKNOWN", "git_tree": "clean"},
        ),
    ],
    ids=["clean", "dirty", "no_git", "not_a_repository"],
)
def test_the_source_is_read_from_local_git_only(
    world: World, head: str | Exception, status: str | Exception, expected: dict[str, str]
) -> None:
    git = FakeGit(head, status)

    assert world.script.source_identity(REPO_ROOT, git) == expected
    assert [argv[3:] for argv in git.argv] == [["rev-parse", "HEAD"], ["status", "--porcelain"]]
    assert all(argv[:3] == ["git", "-C", str(REPO_ROOT)] for argv in git.argv)


def test_the_source_checkout_is_recorded_in_both_files(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = {"git_commit": COMMIT, "git_tree": "dirty"}
    monkeypatch.setattr(world.script, "source_identity", lambda: source)

    assert world.run("--case", "kpis_may_occupancy") == 0

    replays, report = world.files()
    assert replays["live_provenance"]["source"] == report["live_provenance"]["source"] == source


def test_this_checkouts_own_commit_is_what_git_reports() -> None:
    """The real function, on this repository: no network, two local Git commands."""
    found = load_script().source_identity()
    if found["git_commit"] != "UNKNOWN":
        assert len(found["git_commit"]) == 40 and int(found["git_commit"], 16) >= 0
        assert found["git_tree"] in {"clean", "dirty"}


def test_every_model_request_is_timed_in_order_with_how_it_ended(world: World) -> None:
    world.fail_first = 1
    ticks = iter([0.0, 0.25, 1.0, 1.5])

    assert world.run(*three_cases()[:4], monotonic=lambda: next(ticks)) == 0

    _, report = world.files()
    assert report["run_health"]["per_request"] == [
        {
            "case_id": FIRST_THREE[0],
            "request": 1,
            "elapsed_ms": 250,
            "outcome": "LLM_UNAVAILABLE",
            "attempts": None,
        },
        {
            "case_id": FIRST_THREE[1],
            "request": 1,
            "elapsed_ms": 500,
            "outcome": "answered",
            "attempts": 1,
        },
    ]
    # One unavailable request is not a pattern.
    assert report["run_health"]["status"] == "normal"
    assert report["run_health"]["longest_unavailable_streak"] == 1


def test_consecutive_unavailable_requests_mark_the_run_degraded_and_change_no_score(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.fail_first = 2

    assert world.run(*three_cases()) == 0

    _, report = world.files()
    health = report["run_health"]
    assert health["status"] == "degraded"
    assert health["threshold"] == world.script.DEGRADED_AFTER_CONSECUTIVE_UNAVAILABLE == 2
    assert (health["requests"], health["unavailable_requests"]) == (3, 2)
    assert health["degraded_streaks"] == [
        {"first_case": FIRST_THREE[0], "last_case": FIRST_THREE[1], "requests": 2}
    ]
    # The cases are scored exactly as before: health is beside them, never in them.
    assert [case["error_code"] for case in report["cases"]] == ["LLM_UNAVAILABLE"] * 2 + [None]
    assert report["measures"]["completed"] == {"passed": 1, "scored": 3}
    assert all("run_health" not in case for case in report["cases"])
    assert "run health: degraded (2 of 3 model requests unavailable" in capsys.readouterr().out


def test_a_normal_run_says_so(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert world.run("--case", "kpis_may_occupancy") == 0

    _, report = world.files()
    assert report["run_health"]["status"] == "normal"
    assert report["run_health"]["degraded_streaks"] == []
    assert "run health: normal (0 of 1 model requests unavailable" in capsys.readouterr().out


def request_row(case: str, outcome: str) -> dict[str, Any]:
    return {"case_id": case, "request": 1, "elapsed_ms": 1, "outcome": outcome, "attempts": None}


@pytest.mark.parametrize(
    ("outcomes", "status", "longest", "streaks"),
    [
        ([], "normal", 0, []),
        (["LLM_UNAVAILABLE", "answered", "LLM_UNAVAILABLE"], "normal", 1, []),
        # Another declared failure is not unavailability, and breaks a streak.
        (["LLM_UNAVAILABLE", "LLM_RATE_LIMITED", "LLM_UNAVAILABLE"], "normal", 1, []),
        (["LLM_UNAVAILABLE", "LLM_UNAVAILABLE"], "degraded", 2, [("c0", "c1", 2)]),
        (
            ["LLM_UNAVAILABLE"] * 2 + ["answered"] + ["LLM_UNAVAILABLE"] * 3,
            "degraded",
            3,
            [("c0", "c1", 2), ("c3", "c5", 3)],
        ),
    ],
)
def test_run_health_counts_consecutive_unavailable_requests_across_cases(
    world: World,
    outcomes: list[str],
    status: str,
    longest: int,
    streaks: list[tuple[str, str, int]],
) -> None:
    health = world.script.run_health([request_row(f"c{i}", o) for i, o in enumerate(outcomes)])

    assert (health["status"], health["longest_unavailable_streak"]) == (status, longest)
    found = [(s["first_case"], s["last_case"], s["requests"]) for s in health["degraded_streaks"]]
    assert found == streaks
    assert health["version"] == "copilot_run_health_v1"


def test_the_unavailable_code_is_the_one_the_boundary_and_the_circuit_raise(world: World) -> None:
    from app.llm.errors import LlmCircuitOpenError

    assert world.script.UNAVAILABLE_CODE == LlmUnavailableError.code == LlmCircuitOpenError.code


def test_a_stalled_local_provider_is_reported_degraded_through_the_real_boundary(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real factory, guarded boundary and Ollama adapter, over a fake transport that times out
    every attempt -- as the R2.7 pilot's stalled server did. Nothing is contacted."""
    import app.llm.factory as factory
    from app.llm.providers import ollama_provider

    posted: list[str] = []

    def timed_out(_transport: object, url: str, body: bytes, timeout: float) -> bytes:
        posted.append(url)
        raise TimeoutError("timed out")

    monkeypatch.setattr(ollama_provider.UrllibTransport, "post_json", timed_out)
    monkeypatch.setattr(factory, "_BREAKERS", {})  # a fresh breaker, as a new process has
    local(world).real_factory = True

    assert world.run(*three_cases()[:4]) == 0

    _, report = world.files()
    health = report["run_health"]
    assert health["status"] == "degraded"
    assert [(r["case_id"], r["outcome"], r["attempts"]) for r in health["per_request"]] == [
        (FIRST_THREE[0], "LLM_UNAVAILABLE", None),
        (FIRST_THREE[1], "LLM_UNAVAILABLE", None),
    ]
    # One HTTP attempt per request, to the local server only: the boundary retries when ITS OWN
    # deadline expires (§5.7 row 1), not a timeout the adapter itself reports.
    assert posted == ["http://127.0.0.1:11434/api/chat"] * 2
    assert world.built == []  # no stand-in: the real stack ran
    assert report["model_level"]["outcomes"]["unavailable"] == 2


# --- R2.8C: copilot_eval_v3 ---------------------------------------------------------------------


def test_a_v3_set_run_records_v3_and_tells_the_guard_from_the_model(world: World) -> None:
    """A model-scope case reaches the stand-in; a server-guard case never does."""
    from app.llm.prompts.registry import SCOPE_ANSWER
    from tests.evaluation.questions_v3 import COPILOT_EVAL_V3

    local(world).text = SCOPE_ANSWER
    argv = ["--set", "copilot_eval_v3", "--prompt", "copilot_answer@v3"]
    assert world.run(*argv, "--case", "another_property", "--case", "scope_named_competitor") == 0

    replays, report = world.files(V3)
    assert replays["question_set"] == report["question_set"]["identity"] == "copilot_eval_v3"
    selection = report["live_provenance"]["selection"]
    assert (selection["full_set_identity"], selection["full_set_checksum"]) == (
        COPILOT_EVAL_V3.identity,
        COPILOT_EVAL_V3.checksum,
    )
    served = {row["case_id"]: row["answer_source"] for row in report["served"]["cases"]}
    assert served == {"another_property": "scope_guard", "scope_named_competitor": "model"}
    outcomes = {row["case_id"]: row["outcome"] for row in report["model_level"]["cases"]}
    assert outcomes == {"another_property": "not_asked", "scope_named_competitor": "final_response"}
    assert world.built[0].calls == 1  # only the model-scope case was asked


# --- R2.8L: the integrity block -------------------------------------------------------------------


def test_a_v3_set_run_carries_the_integrity_block(world: World) -> None:
    from app.llm.prompts.registry import SCOPE_ANSWER
    from tests.evaluation.integrity import INTEGRITY_VERSION

    local(world).text = SCOPE_ANSWER
    argv = ["--set", "copilot_eval_v3", "--prompt", "copilot_answer@v3"]
    assert world.run(*argv, "--case", "scope_named_competitor") == 0

    _, report = world.files(V3)
    assert report["integrity"]["version"] == INTEGRITY_VERSION
    [row] = report["integrity"]["cases"]
    assert (row["refusal_result"], row["scope_compliance"]) == ("correct", True)


def test_a_run_on_a_set_without_expectations_has_no_integrity_block(world: World) -> None:
    assert world.run("--set", "copilot_eval_v2", "--case", "kpis_may_occupancy") == 0

    _, report = world.files()
    assert "integrity" not in report
    assert "model_level" in report  # the other blocks are unchanged


# --- R2.8N-G: the server_guard block -------------------------------------------------------------


def test_a_v3_run_carries_the_versioned_server_guard_block(world: World) -> None:
    """The script asks for `copilot_eval_v3_expectations_v2` on a v3 run: the bare UUID case is
    refused before the stand-in is asked, beside R2.8J's recorded model outcome."""
    from app.llm.prompts.registry import SCOPE_ANSWER
    from tests.evaluation.transcribed_r28j import TURNS

    local(world).text = SCOPE_ANSWER
    argv = ["--set", "copilot_eval_v3", "--prompt", "copilot_answer@v3"]
    cases = ["--case", "scope_bare_property_uuid", "--case", "scope_named_competitor"]
    assert world.run(*argv, *cases) == 0

    _, report = world.files(V3)
    block = report["server_guard"]
    assert (block["version"], block["full_set"]) == ("copilot_eval_v3_expectations_v2", False)
    [row] = block["cases"]
    assert row["case_id"] == "scope_bare_property_uuid"
    assert row["guard_verdict"] == "refused_before_model"
    assert row["model_side"] == {"model_responses": 0, "model_text_present": False}
    assert row["history"]["model_text"] == TURNS["scope_bare_property_uuid"][-1]["text"]
    assert block["not_scored"] == ["scope_named_competitor"]
    assert world.built[0].calls == 1  # only the model-scope case reached the stand-in
    assert {"served", "model_level", "integrity"} <= set(report)  # beside, not instead


@pytest.mark.parametrize(
    "argv",
    [
        ["--set", "copilot_eval_v2", "--case", "kpis_may_occupancy"],
        ["--case", "kpis_may_occupancy"],  # v1, the default set
    ],
)
def test_an_earlier_set_run_has_no_server_guard_block(world: World, argv: list[str]) -> None:
    assert world.run(*argv) == 0

    _, report = world.files()
    assert "server_guard" not in report
    assert "model_level" in report  # the other blocks are unchanged


def test_a_knowledge_run_has_no_server_guard_block(world: World) -> None:
    case = COPILOT_KNOWLEDGE_EVAL_V1.cases[0].case_id
    assert world.run("--set", "copilot_knowledge_eval_v1", "--case", case) == 0

    _, report = world.files()
    assert "server_guard" not in report


# --- R2.8M: invalid-response diagnostics in run_health ------------------------------------------

DIAGNOSTIC_MARKER = "DIAG-CONTENT-MARKER-7f3a"


class Raising:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def complete(self, request: Any) -> Any:
        raise self.error


def test_only_an_invalid_response_row_carries_diagnostics(world: World) -> None:
    from app.llm.errors import InvalidResponseDiagnostics, LlmInvalidResponseError

    log: list[dict[str, Any]] = []
    rejected = LlmInvalidResponseError(
        diagnostics=InvalidResponseDiagnostics("empty_response", provider="ollama")
    )
    for error in (rejected, LlmUnavailableError()):
        with pytest.raises(type(error)):
            world.script.TimedModel(Raising(error), log, "c").complete(object())

    invalid, unavailable = log
    assert invalid["outcome"] == "LLM_INVALID_RESPONSE"
    assert invalid["diagnostics"] == rejected.diagnostics.as_dict()
    assert "diagnostics" not in unavailable  # every other row keeps its shape
    # run_health reads both, and rows from before R2.8M (no key at all) as before.
    assert world.script.run_health(log)["status"] == "normal"


def test_a_rejected_answer_is_diagnosed_end_to_end_without_its_content(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real factory, boundary and Ollama adapter over a fake transport. The body is a
    SYNTHETIC answer shaped like R2.8J's failure -- tool calls cut off at 1,024 tokens -- not the
    recorded one, which was never kept."""
    import app.llm.factory as factory
    from app.llm.providers import ollama_provider

    call = {"function": {"name": "get_demand_forecast", "arguments": {"q": DIAGNOSTIC_MARKER}}}
    body = json.dumps(
        {
            "done": True,
            "done_reason": "length",
            "eval_count": 1024,
            "message": {"content": "", "tool_calls": [call, call]},
        }
    ).encode("utf-8")

    def answer(_transport: object, url: str, sent: bytes, timeout: float) -> bytes:
        return body

    monkeypatch.setattr(ollama_provider.UrllibTransport, "post_json", answer)
    monkeypatch.setattr(factory, "_BREAKERS", {})
    local(world).real_factory = True

    assert world.run("--case", "kpis_may_occupancy") == 0

    replays, report = world.files()
    [row] = report["run_health"]["per_request"]
    assert (row["outcome"], row["attempts"]) == ("LLM_INVALID_RESPONSE", None)
    assert row["diagnostics"] == {
        "reason": "length_with_tool_calls",
        "provider": "ollama",
        "completion_reason": "length",
        "output_tokens": 1024,
        "tool_calls_parsed": 2,
        "has_tool_calls": True,
        "has_text": False,
        "structural": True,
    }
    [case] = report["cases"]
    assert case["error_code"] == "LLM_INVALID_RESPONSE"  # the failure's classification is unchanged
    for written in (json.dumps(replays), json.dumps(report)):
        assert DIAGNOSTIC_MARKER not in written


# --- R2.8N-W: derived diagnostic annotations ----------------------------------------------------


def cut_off_with_calls(
    world: World, monkeypatch: pytest.MonkeyPatch, calls: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One `kpis_may_occupancy` run whose answer is cut off at 1,024 tokens while holding *calls*
    tool calls: the real factory, boundary and Ollama adapter over a fake transport. SYNTHETIC."""
    import app.llm.factory as factory
    from app.llm.providers import ollama_provider

    call = {"function": {"name": "get_demand_forecast", "arguments": {"q": DIAGNOSTIC_MARKER}}}
    body = json.dumps(
        {
            "done": True,
            "done_reason": "length",
            "eval_count": 1024,
            "message": {"content": "", "tool_calls": [call] * calls},
        }
    ).encode("utf-8")

    def answer(_transport: object, url: str, sent: bytes, timeout: float) -> bytes:
        return body

    monkeypatch.setattr(ollama_provider.UrllibTransport, "post_json", answer)
    monkeypatch.setattr(factory, "_BREAKERS", {})
    local(world).real_factory = True
    assert world.run("--case", "kpis_may_occupancy") == 0
    return world.files()


def test_a_cut_off_answer_over_the_call_cap_is_annotated_and_nothing_else_changes(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.copilot.loop import MAX_CALLS_PER_ROUND

    replays, report = cut_off_with_calls(world, monkeypatch, MAX_CALLS_PER_ROUND + 1)

    block = report["diagnostic_annotations"]
    assert block["version"] == "copilot_eval_diagnostic_annotations_v1"
    assert block["annotations"] == [
        {
            "case_id": "kpis_may_occupancy",
            "request": 1,
            "annotation": "call_cap_exceeded_before_truncation",
            "evidence": {
                "reason": "length_with_tool_calls",
                "tool_calls_parsed": MAX_CALLS_PER_ROUND + 1,
            },
        }
    ]
    # The raw record, the case, and the model-level outcome are exactly as without it.
    [row] = report["run_health"]["per_request"]
    assert row["diagnostics"]["tool_calls_parsed"] == MAX_CALLS_PER_ROUND + 1
    [case] = report["cases"]
    assert case["error_code"] == "LLM_INVALID_RESPONSE"
    [level] = report["model_level"]["cases"]
    assert level["outcome"] == "unavailable"
    for written in (json.dumps(replays), json.dumps(report)):
        assert DIAGNOSTIC_MARKER not in written  # still no argument anywhere


def test_a_cut_off_answer_within_the_call_cap_is_not_annotated(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, report = cut_off_with_calls(world, monkeypatch, 2)
    assert report["run_health"]["per_request"][0]["diagnostics"]["reason"] == (
        "length_with_tool_calls"
    )
    assert report["diagnostic_annotations"]["annotations"] == []
    # The block's presence changes nothing either: the same outcomes as the over-cap case.
    [case] = report["cases"]
    assert case["error_code"] == "LLM_INVALID_RESPONSE"
    [level] = report["model_level"]["cases"]
    assert level["outcome"] == "unavailable"


def test_an_ordinary_run_carries_an_empty_annotation_block(world: World) -> None:
    assert world.run("--case", "kpis_may_occupancy") == 0
    _, report = world.files()
    assert report["diagnostic_annotations"]["annotations"] == []


# --- R2.9B: the R2.8AA scoring blocks in a live v3 run ---------------------------------------

#: The two R2.8AA flags `harness.run` takes. A live run asks for them on a v3 set only.
SCORING_V2_FLAGS = ("model_level_v2", "integrity_v3")


def spy_on_runners(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record the keyword arguments the script passes to either harness, then run it for real."""
    from tests.evaluation import harness, knowledge_harness

    calls: list[dict[str, Any]] = []
    for module in (harness, knowledge_harness):
        real = module.run

        def spy(*args: Any, _real: Any = real, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return _real(*args, **kwargs)

        monkeypatch.setattr(module, "run", spy)
    return calls


class ToolThenInvalid:
    """Asks for the forecast tool, then fails the next request as R2.8X's v3 `inject_estimate`
    did: `LLM_INVALID_RESPONSE`, a reply cut off while writing tool calls. SYNTHETIC."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request: ChatRequest) -> ChatResponse:
        from app.llm.base import ToolCall
        from app.llm.errors import InvalidResponseDiagnostics, LlmInvalidResponseError

        self.calls += 1
        if self.calls > 1:
            raise LlmInvalidResponseError(
                diagnostics=InvalidResponseDiagnostics("length_with_tool_calls", provider="ollama")
            )
        call = ToolCall(
            id="call-1", name="get_demand_forecast", arguments={"target_date": "2026-06-01"}
        )
        return ChatResponse(
            text="",
            parsed=None,
            usage=TokenUsage(input_tokens=3, output_tokens=2),
            latency_ms=0.0,
            provider="ollama",
            model=CONFIGURED,
            finish_reason="tool_use",
            prompt_id=request.prompt_id,
            prompt_version=request.prompt_version,
            tool_calls=(call,),
        )


def test_a_v3_run_asks_the_harness_for_both_r2_8aa_blocks(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = spy_on_runners(monkeypatch)
    argv = ["--set", "copilot_eval_v3", "--prompt", "copilot_answer@v3"]
    assert local(world).run(*argv, "--case", "kpis_may_occupancy") == 0

    [options] = calls
    assert {flag: options.get(flag) for flag in SCORING_V2_FLAGS} == {
        "model_level_v2": True,
        "integrity_v3": True,
    }
    # Beside the blocks a v3 run already asked for, never instead of them.
    assert all(options[flag] is True for flag in ("served", "model_level", "integrity"))
    assert options["server_guard"] is True


def test_a_full_v3_run_carries_both_blocks_with_their_versions_and_policies(
    world: World,
) -> None:
    from app.llm.prompts.registry import CANNOT_ANSWER
    from tests.evaluation.integrity_v3 import INTEGRITY_V3_VERSION
    from tests.evaluation.model_level_v2 import MODEL_LEVEL_V2_VERSION
    from tests.evaluation.questions_v3 import COPILOT_EVAL_V3

    local(world).text = CANNOT_ANSWER
    assert world.run("--set", "copilot_eval_v3", "--prompt", "copilot_answer@v3") == 0

    _, report = world.files(V3)
    level, integrity = report["model_level_v2"], report["integrity_v3"]
    assert (level["version"], integrity["version"]) == (
        MODEL_LEVEL_V2_VERSION,
        INTEGRITY_V3_VERSION,
    )
    ids = [case.case_id for case in COPILOT_EVAL_V3.cases]
    assert [row["case_id"] for row in level["cases"]] == ids
    assert [row["case_id"] for row in integrity["cases"]] == ids
    # The three server-guard cases never reach the model; every other case answered once.
    assert level["outcomes"] == {
        "final_response": 26,
        "invalid_response": 0,
        "not_asked": 3,
        "tool_stopped": 0,
        "unavailable": 0,
    }
    rows = {row["case_id"]: row for row in level["cases"]}
    # Decision A: the exact cannot-answer sentence is a correct answer to inject_estimate.
    estimate = rows["inject_estimate"]
    assert (estimate["answer_policy"], estimate["forecast_case_correct"]) == (
        "forecast_permitted",
        True,
    )
    # Decision B: review_text needs a document search, and the stand-in made none.
    review = rows["review_text"]
    assert review["required_tools_missing"] == ["search_hotel_knowledge"]
    assert review["model_refusal_correct"] is False
    # The legacy blocks stay beside the new ones, for comparison.
    assert {"served", "model_level", "integrity", "server_guard"} <= set(report)


def test_a_v3_case_selection_scores_only_the_selected_cases(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = spy_on_runners(monkeypatch)
    argv = ["--set", "copilot_eval_v3", "--prompt", "copilot_answer@v3"]
    assert local(world).run(*argv, "--case", "inject_estimate", "--case", "review_text") == 0

    [options] = calls
    assert all(options.get(flag) is True for flag in SCORING_V2_FLAGS)
    _, report = world.files(V3)
    for name in SCORING_V2_FLAGS:
        assert [row["case_id"] for row in report[name]["cases"]] == [
            "review_text",
            "inject_estimate",
        ]


@pytest.mark.parametrize(
    "argv",
    [
        ["--case", "kpis_may_occupancy"],  # v1, the default set
        ["--set", "copilot_eval_v2", "--case", "kpis_may_occupancy"],
        [
            "--set",
            "copilot_knowledge_eval_v1",
            "--case",
            COPILOT_KNOWLEDGE_EVAL_V1.cases[0].case_id,
        ],
    ],
    ids=["v1", "v2", "knowledge"],
)
def test_an_earlier_or_knowledge_set_run_asks_for_neither_r2_8aa_block(
    world: World, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    calls = spy_on_runners(monkeypatch)
    assert world.run(*argv) == 0

    [options] = calls
    assert not any(options.get(flag) for flag in SCORING_V2_FLAGS)
    _, report = world.files()
    assert not set(SCORING_V2_FLAGS) & set(report)


def test_a_failure_after_a_tool_round_is_an_invalid_response_in_model_level_v2(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.llm.factory as factory

    monkeypatch.setattr(factory, "build_chat_model", lambda _settings: ToolThenInvalid())
    argv = ["--set", "copilot_eval_v3", "--prompt", "copilot_answer@v3"]
    assert local(world).run(*argv, "--case", "inject_estimate") == 0

    _, report = world.files(V3)
    level = report["model_level_v2"]
    [row] = level["cases"]
    assert (row["outcome"], row["model_error_code"]) == ("invalid_response", "LLM_INVALID_RESPONSE")
    assert level["model_error_codes"] == {"LLM_INVALID_RESPONSE": 1}
    # A model-quality failure of the case's strict measure: scored, and not excluded.
    assert row["forecast_case_correct"] is False
    measure = level["measures"]["forecast_case_correct"]
    assert (measure["passed"], measure["scored"], measure["excluded"]) == (0, 1, {})
    [served] = report["integrity_v3"]["cases"]
    assert served["outcome"] == "invalid_response"
    # The legacy readings are unchanged: the v1 model level calls it unavailable.
    assert report["model_level"]["cases"][0]["outcome"] == "unavailable"
    assert [(r["request"], r["outcome"]) for r in report["run_health"]["per_request"]] == [
        (1, "answered"),
        (2, "LLM_INVALID_RESPONSE"),
    ]


def test_the_key_is_in_no_file_and_no_output_of_a_v3_run(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["--set", "copilot_eval_v3", "--case", "inject_estimate", "--case", "review_text"]
    assert world.run(*argv) == 0  # the paid path: the sentinel key is configured

    printed = capsys.readouterr()
    _, report = world.files()
    assert set(SCORING_V2_FLAGS) <= set(report)
    written = list(world.out.iterdir())
    assert len(written) == 2
    for path in written:
        assert SENTINEL_KEY not in path.read_text(encoding="utf-8")
    assert SENTINEL_KEY not in printed.out
    assert SENTINEL_KEY not in printed.err


# --- R2.9C: the same policy provenance live and in replay ----------------------------------------


def test_a_live_v3_report_and_a_replay_of_its_capture_record_the_same_policy_provenance(
    world: World,
) -> None:
    """The live report names its policies; replaying the capture it wrote, with the same blocks,
    names exactly the same ones. The capture itself holds turns only: scoring, and the policies it
    was done under, belong to a report."""
    from dataclasses import replace

    from app.llm.prompts.registry import COPILOT_ANSWER_V3
    from tests.evaluation import harness
    from tests.evaluation.case_policy_v3 import CASE_POLICY_VERSION
    from tests.evaluation.forecast_policy import FORECAST_POLICY_VERSION
    from tests.evaluation.questions_v3 import COPILOT_EVAL_V3
    from tests.evaluation.replay import ReplayModel, load_replays

    argv = ["--set", "copilot_eval_v3", "--prompt", "copilot_answer@v3"]
    selection = ["--case", "guest_names", "--case", "inject_estimate"]
    assert local(world).run(*argv, *selection) == 0
    replays, report = world.files(V3)

    assert report["model_level_v2"]["policies"] == {
        "case_policy": {"version": CASE_POLICY_VERSION},
        "forecast_policy": {"version": FORECAST_POLICY_VERSION, "applies_to": ["inject_estimate"]},
    }
    assert not {"policies", "model_level_v2", "integrity_v3"} & set(replays)

    subset = replace(
        COPILOT_EVAL_V3,
        cases=tuple(
            c for c in COPILOT_EVAL_V3.cases if c.case_id in {"guest_names", "inject_estimate"}
        ),
    )
    recorded = load_replays(
        world.out / f"replays-{TODAY}{V3}.json", subset, prompt=COPILOT_ANSWER_V3
    )
    replayed = harness.run(
        subset,
        lambda case: ReplayModel(recorded.cases[case.case_id]),
        mode="replay",
        provider=recorded.provider,
        model=recorded.model,
        recorded_on=recorded.recorded_on,
        prompt=COPILOT_ANSWER_V3,
        model_level_v2=True,
        integrity_v3=True,
    )
    for block in ("model_level_v2", "integrity_v3"):
        assert replayed[block]["policies"] == report[block]["policies"]


# --- R2.9H: repeated runs ------------------------------------------------------------------------

#: The live_provenance keys a single run recorded before R2.9H. A single run under any provider but
#: `ollama` still records exactly these; an `ollama` one adds `local_model`, and nothing else.
SINGLE_RUN_PROVENANCE_KEYS = {
    "source",
    "provider",
    "configured_model",
    "prompt",
    "budget",
    "thinking",
    "returned_models",
    "returned_models_by_case",
    "sdk_distribution",
    "sdk_version",
    "ollama_options",
    "selection",
}
V4_ARGS = ["--set", "copilot_eval_v3", "--prompt", "copilot_answer@v4"]
REPEATED_CASES = ["--case", "kpis_may_occupancy", "--case", "another_property"]


class NumberedModel:
    """Answers without tools, naming the request it answers -- so every attempt's transcript is
    its own -- and raises `LlmUnavailableError` on the requests numbered in *unavailable*."""

    def __init__(self, unavailable: frozenset[int] = frozenset()) -> None:
        self.unavailable = unavailable
        self.calls = 0

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.calls += 1
        if self.calls in self.unavailable:
            raise LlmUnavailableError()
        return ChatResponse(
            text=f"Answer number {self.calls}.",
            parsed=None,
            usage=TokenUsage(input_tokens=3, output_tokens=2),
            latency_ms=0.0,
            provider="ollama",
            model=CONFIGURED,
            finish_reason="stop",
            prompt_id=request.prompt_id,
            prompt_version=request.prompt_version,
        )


def repeated(world: World, monkeypatch: pytest.MonkeyPatch, model: NumberedModel) -> World:
    import app.llm.factory as factory

    monkeypatch.setattr(factory, "build_chat_model", lambda _settings: model)
    return local(world)


def written(world: World) -> list[str]:
    return sorted(path.name for path in world.out.iterdir())


def test_the_default_repeat_is_one_and_writes_exactly_the_two_files_it_always_did(
    world: World,
) -> None:
    assert world.script.parse_arguments(["--out", "x", "--confirm-local-model-call"]).repeat == 1
    assert world.run("--case", "kpis_may_occupancy") == 0
    assert written(world) == [f"replays-{TODAY}.json", f"report-{TODAY}.json"]
    replays, report = world.files()
    for provenance in (replays["live_provenance"], report["live_provenance"]):
        assert set(provenance) == SINGLE_RUN_PROVENANCE_KEYS  # anthropic: no new key at all


@pytest.mark.parametrize("value", ["0", "-1", "11", "1.5", "two", "", "01", "1e1"])
def test_an_invalid_repeat_is_refused_before_a_client_exists(
    world: World, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    with pytest.raises(SystemExit) as refused:
        world.run("--repeat", value)
    assert refused.value.code == 2
    assert "--repeat must be a whole number from 1 to 10" in capsys.readouterr().err
    assert world.built == []
    assert not world.out.exists()


@pytest.mark.parametrize("value", ["1", "2", "10", " 3 ", "+4"])
def test_a_valid_repeat_is_accepted(value: str) -> None:
    script = load_script()
    parsed = script.parse_arguments(["--out", "x", "--confirm-local-model-call", "--repeat", value])
    assert parsed.repeat == int(value)


def test_attempt_identities_and_file_names_are_deterministic_and_distinct() -> None:
    script = load_script()
    ids = [script.attempt_id("2026-11-02", "copilot_answer@v4", k, 3) for k in (1, 2, 3)]
    assert ids == [
        "2026-11-02-copilot_answer-v4-attempt-1-of-3",
        "2026-11-02-copilot_answer-v4-attempt-2-of-3",
        "2026-11-02-copilot_answer-v4-attempt-3-of-3",
    ]
    assert ids == [script.attempt_id("2026-11-02", "copilot_answer@v4", k, 3) for k in (1, 2, 3)]
    # The default prompt is named in the identity even though its file names omit it.
    assert script.attempt_id("2026-11-02", "copilot_answer@v2", 1, 2) == (
        "2026-11-02-copilot_answer-v2-attempt-1-of-2"
    )
    out = Path("o")
    assert script.output_paths(out, "2026-11-02", "copilot_answer@v4", 2) == (
        out / "replays-2026-11-02-copilot_answer-v4-attempt-2.json",
        out / "report-2026-11-02-copilot_answer-v4-attempt-2.json",
    )
    assert script.output_paths(out, "2026-11-02", "copilot_answer@v4") == (
        out / "replays-2026-11-02-copilot_answer-v4.json",
        out / "report-2026-11-02-copilot_answer-v4.json",
    )
    assert script.repeats_path(out, "2026-11-02", "copilot_answer@v4") == (
        out / "repeats-2026-11-02-copilot_answer-v4.json"
    )


def test_a_repeated_run_writes_one_standard_pair_per_attempt_and_an_aggregate(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from app.llm.prompts.registry import COPILOT_ANSWER_V4
    from tests.evaluation.questions_v3 import COPILOT_EVAL_V3
    from tests.evaluation.replay import load_replays

    model = NumberedModel()
    assert repeated(world, monkeypatch, model).run(*V4_ARGS, *REPEATED_CASES, "--repeat", "3") == 0
    stem = f"{TODAY}{V4}"
    assert written(world) == sorted(
        [f"replays-{stem}-attempt-{k}.json" for k in (1, 2, 3)]
        + [f"report-{stem}-attempt-{k}.json" for k in (1, 2, 3)]
        + [f"repeats-{stem}.json"]
    )
    assert model.calls == 3  # one model case per attempt; the server-guard case asks none
    subset = replace(
        COPILOT_EVAL_V3,
        cases=tuple(c for c in COPILOT_EVAL_V3.cases if c.case_id in REPEATED_CASES),
    )
    for k in (1, 2, 3):
        replays, report = world.files(f"{V4}-attempt-{k}")
        identity = f"{TODAY}-copilot_answer-v4-attempt-{k}-of-3"
        expected = {"repeat": 3, "attempt": k, "attempt_id": identity}
        assert replays["live_provenance"]["repetition"] == expected
        assert report["live_provenance"]["repetition"] == expected
        # Each attempt's transcript is its own, and loads as any capture does.
        assert replays["cases"]["kpis_may_occupancy"][0]["text"] == f"Answer number {k}."
        loaded = load_replays(
            world.out / f"replays-{stem}-attempt-{k}.json", subset, prompt=COPILOT_ANSWER_V4
        )
        assert loaded.cases["kpis_may_occupancy"][0].text == f"Answer number {k}."
        # Each attempt's report is a whole report, with every block and its own run health.
        assert {"model_level_v2", "integrity_v3", "served", "run_health"} <= set(report)
        assert report["run_health"]["requests"] == 1


def test_the_aggregate_agrees_with_every_attempts_own_report(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        repeated(world, monkeypatch, NumberedModel()).run(
            *V4_ARGS, *REPEATED_CASES, "--repeat", "2"
        )
        == 0
    )
    stem = f"{TODAY}{V4}"
    aggregate = json.loads((world.out / f"repeats-{stem}.json").read_text(encoding="utf-8"))
    assert (aggregate["format"], aggregate["repeat"]) == ("copilot_eval_repeats_v1", 2)
    assert aggregate["prompt"]["identity"] == "copilot_answer@v4"
    for entry in aggregate["attempts"]:
        k = entry["attempt"]
        for kind in ("replay", "report"):
            raw = (world.out / entry[f"{kind}_file"]).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == entry[f"{kind}_sha256"]
        _, report = world.files(f"{V4}-attempt-{k}")
        level = {row["case_id"]: row for row in report["model_level_v2"]["cases"]}
        served = {row["case_id"]: row for row in report["served"]["cases"]}
        mine = [row for row in aggregate["rows"] if row["attempt"] == k]
        assert [row["case_id"] for row in mine] == [c["case_id"] for c in report["cases"]]
        for row in mine:
            assessed = level[row["case_id"]]
            assert row["model_outcome"] == assessed["outcome"]
            assert row["answer_source"] == served[row["case_id"]]["answer_source"]
            assert row["strict_result"] == assessed[row["strict_measure"]]
            assert row["server_intervened"] == assessed["server_intervened"]
    for case_id, summary in aggregate["cases"].items():
        rows = [row for row in aggregate["rows"] if row["case_id"] == case_id]
        assert summary["attempts"] == len(rows) == 2
        assert sum(summary["model_outcomes"].values()) == 2
        strict = summary["strict"]
        assert strict["passed"] + strict["failed"] + sum(strict["not_scored"].values()) == 2
    # Model outcome and served answer stay apart: the guard case was served, never asked.
    guard = aggregate["cases"]["another_property"]
    assert guard["model_outcomes"] == {"not_asked": 2}
    assert guard["answer_sources"] == {"scope_guard": 2}
    assert guard["strict"]["not_scored"] == {"not_asked": 2}


def test_a_failed_attempt_does_not_erase_the_others(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second attempt's only model request is unavailable: that attempt records the failure,
    the first and third keep their answers, and the aggregate counts all three."""
    model = NumberedModel(unavailable=frozenset({2}))
    assert repeated(world, monkeypatch, model).run(*V4_ARGS, *REPEATED_CASES, "--repeat", "3") == 0
    aggregate = json.loads((world.out / f"repeats-{TODAY}{V4}.json").read_text(encoding="utf-8"))
    by_attempt = {
        row["attempt"]: row for row in aggregate["rows"] if row["case_id"] == "kpis_may_occupancy"
    }
    assert {k: row["model_outcome"] for k, row in by_attempt.items()} == {
        1: "final_response",
        2: "unavailable",
        3: "final_response",
    }
    assert by_attempt[2]["error_code"] == "LLM_UNAVAILABLE"
    assert aggregate["cases"]["kpis_may_occupancy"]["model_outcomes"] == {
        "final_response": 2,
        "unavailable": 1,
    }
    _, failed = world.files(f"{V4}-attempt-2")
    assert failed["run_health"]["unavailable_requests"] == 1
    replays, _ = world.files(f"{V4}-attempt-3")
    assert replays["cases"]["kpis_may_occupancy"][0]["text"] == "Answer number 3."
    assert model.calls == 3  # nothing was retried beyond the existing policy


def test_a_repeated_run_refuses_an_output_that_holds_any_attempts_file(world: World) -> None:
    world.out.mkdir(parents=True)
    (world.out / f"report-{TODAY}-attempt-2.json").write_text("{}", encoding="utf-8")
    assert world.run("--case", "kpis_may_occupancy", "--repeat", "2") == 2
    assert world.built == []


# --- R2.9H: local model provenance ---------------------------------------------------------------

MANIFEST = {
    "schemaVersion": 2,
    "layers": [
        {
            "mediaType": "application/vnd.ollama.image.model",
            "digest": "sha256:" + "ab" * 32,
            "size": 123,
        },
        {
            "mediaType": "application/vnd.ollama.image.license",
            "digest": "sha256:" + "cd" * 32,
            "size": 9,
        },
    ],
}


def write_manifest(models: Path, relative: str, content: bytes) -> Path:
    path = models / "manifests" / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    return path


def test_the_local_manifest_gives_the_manifest_and_model_layer_digests(tmp_path: Path) -> None:
    script = load_script()
    raw = json.dumps(MANIFEST).encode("utf-8")
    write_manifest(tmp_path, "registry.ollama.ai/library/qwen3.5/4b", raw)
    digest = hashlib.sha256(raw).hexdigest()
    assert script.ollama_manifest("qwen3.5:4b", tmp_path) == {
        "status": "available",
        "source": "local_manifest",
        "manifest_digest": f"sha256:{digest}",
        "manifest_id": digest[:12],
        "model_layer_digest": "sha256:" + "ab" * 32,
        "model_layer_bytes": 123,
    }
    # A namespaced model and an untagged one ("latest") are found where Ollama keeps them.
    write_manifest(tmp_path, "registry.ollama.ai/someone/tiny/latest", raw)
    assert script.ollama_manifest("someone/tiny", tmp_path)["status"] == "available"


@pytest.mark.parametrize(
    ("model", "content", "expected"),
    [
        ("qwen3.5:4b", None, {"status": "unavailable", "reason": "manifest_not_found"}),
        ("../escape:4b", None, {"status": "unavailable", "reason": "unrecognised_model_name"}),
        ("a/b/c/d:4b", None, {"status": "unavailable", "reason": "unrecognised_model_name"}),
    ],
    ids=["absent", "path_traversal", "too_many_parts"],
)
def test_an_unreadable_manifest_is_unavailable_never_guessed(
    tmp_path: Path, model: str, content: bytes | None, expected: dict[str, str]
) -> None:
    assert load_script().ollama_manifest(model, tmp_path) == expected


def test_a_malformed_manifest_keeps_its_digest_and_marks_the_layer_unavailable(
    tmp_path: Path,
) -> None:
    script = load_script()
    write_manifest(tmp_path, "registry.ollama.ai/library/qwen3.5/4b", b"not json")
    found = script.ollama_manifest("qwen3.5:4b", tmp_path)
    assert found["manifest_digest"] == "sha256:" + hashlib.sha256(b"not json").hexdigest()
    assert found["model_layer"] == "unavailable"
    assert "model_layer_digest" not in found


class VersionSpy:
    """Stands in for the HTTP GET: records every URL asked for, and answers or raises."""

    def __init__(self, reply: bytes | Exception) -> None:
        self.reply = reply
        self.urls: list[tuple[str, float]] = []

    def __call__(self, url: str, timeout: float) -> bytes:
        self.urls.append((url, timeout))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (
            b'{"version": "0.40.11"}',
            {"status": "available", "source": "GET /api/version", "version": "0.40.11"},
        ),
        (ConnectionRefusedError(), {"status": "unavailable", "reason": "unreachable"}),
        (b"<html>", {"status": "unavailable", "reason": "malformed_reply"}),
        (b'{"version": ""}', {"status": "unavailable", "reason": "malformed_reply"}),
        (
            b'{"version": "' + b"9" * 5000 + b'"}',
            {"status": "unavailable", "reason": "malformed_reply"},
        ),
    ],
    ids=["answered", "refused", "not_json", "empty", "oversized"],
)
def test_the_server_version_is_read_from_the_version_endpoint_only(
    reply: bytes | Exception, expected: dict[str, str]
) -> None:
    spy = VersionSpy(reply)
    assert load_script().ollama_server_version("http://127.0.0.1:11434/", spy) == expected
    assert spy.urls == [("http://127.0.0.1:11434/api/version", 5.0)]


def test_the_version_request_is_a_body_less_get_over_the_loopback_opener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No generation request: one GET to /api/version, with no body, through the adapter's own
    opener (no proxy, no redirect). The opener is a stand-in -- no socket is opened."""
    from app.llm.providers import ollama_provider

    sent: list[Any] = []

    class Opener:
        def open(self, request: Any, timeout: float) -> Any:
            sent.append((request, timeout))

            class Response:
                def __enter__(self) -> Any:
                    return self

                def __exit__(self, *exc: object) -> None:
                    return None

                def read(self, limit: int) -> bytes:
                    return b'{"version": "0.40.11"}'

            return Response()

    monkeypatch.setattr(ollama_provider, "build_loopback_opener", lambda *handlers: Opener())
    script = load_script()
    assert script.ollama_server_version("http://127.0.0.1:11434")["version"] == "0.40.11"
    [(request, timeout)] = sent
    assert (request.get_method(), request.full_url, request.data) == (
        "GET",
        "http://127.0.0.1:11434/api/version",
        None,
    )
    assert timeout == 5.0


def test_local_provenance_asks_no_model_and_is_absent_for_the_paid_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The only request local provenance makes is the version GET; the manifest is read from disk
    (here an empty models directory, so it is unavailable). A paid run makes none at all."""
    script = load_script()
    spy = VersionSpy(b'{"version": "0.40.11"}')
    monkeypatch.setattr(script, "_get_version", spy)
    monkeypatch.setattr(script, "ollama_models_dir", lambda: tmp_path)
    local_settings = Settings(
        _env_file=None, environment="test", llm_enabled=True, llm_provider="ollama"
    )
    found = script.local_model_provenance(local_settings)
    assert found == {
        "model": "qwen3.5:4b",
        "server_version": {
            "status": "available",
            "source": "GET /api/version",
            "version": "0.40.11",
        },
        "manifest": {"status": "unavailable", "reason": "manifest_not_found"},
    }
    assert spy.urls == [("http://127.0.0.1:11434/api/version", 5.0)]
    paid = Settings(
        _env_file=None,
        environment="test",
        llm_enabled=True,
        llm_provider="anthropic",
        llm_api_key=SENTINEL_KEY,
    )
    assert script.local_model_provenance(paid) is None
    assert len(spy.urls) == 1  # the paid provider's run asked nothing for it


def test_a_local_run_records_its_local_model_and_a_paid_run_does_not(world: World) -> None:
    world.local_model = {
        "model": "qwen3.5:4b",
        "server_version": {
            "status": "available",
            "source": "GET /api/version",
            "version": "0.40.11",
        },
        "manifest": {
            "status": "available",
            "source": "local_manifest",
            "manifest_id": "2a654d98e6fb",
        },
    }
    assert local(world).run("--case", "kpis_may_occupancy") == 0
    replays, report = world.files()
    for provenance in (replays["live_provenance"], report["live_provenance"]):
        assert set(provenance) == SINGLE_RUN_PROVENANCE_KEYS | {"local_model"}
        assert provenance["local_model"] == world.local_model
    assert "repetition" not in report["live_provenance"]  # a single run records no repetition


# --- R2.9X: --case-policy selects the case-policy version a copilot_eval_v3 run is scored under ---


def test_the_case_policy_choices_are_the_registered_versions_and_v1_is_the_default() -> None:
    from tests.evaluation.case_policy_v3 import CASE_POLICY_VERSION, CASE_POLICY_VERSIONS

    script = load_script()
    assert tuple(CASE_POLICY_VERSIONS) == script.CASE_POLICIES
    assert script.DEFAULT_CASE_POLICY == CASE_POLICY_VERSION
    assert script.parse_arguments(["--out", "x", "--confirm-local-model-call"]).case_policy == (
        CASE_POLICY_VERSION
    )
    with pytest.raises(SystemExit):
        script.parse_arguments(["--out", "x", "--confirm-local-model-call", "--case-policy", "v2"])


@pytest.mark.parametrize(
    ("argv", "version"),
    [
        ((), "copilot_eval_v3_case_policy_v1"),
        (("--case-policy", "copilot_eval_v3_case_policy_v2"), "copilot_eval_v3_case_policy_v2"),
    ],
    ids=["default", "v2"],
)
def test_a_v3_run_records_the_case_policy_it_was_scored_under(
    world: World, argv: tuple[str, ...], version: str
) -> None:
    from app.llm.prompts.registry import CANNOT_ANSWER

    local(world).text = CANNOT_ANSWER
    assert world.run("--set", "copilot_eval_v3", "--case", "review_text", *argv) == 0
    _, report = world.files()
    for block in ("model_level_v2", "integrity_v3"):
        assert report[block]["policies"]["case_policy"] == {"version": version}, block
    [row] = report["model_level_v2"]["cases"]
    # Under v1 the search is required; under v2 a no-tool cannot-answer decline is a pass.
    assert row["model_refusal_correct"] is (version == "copilot_eval_v3_case_policy_v2")


def test_naming_v2_for_a_set_without_a_case_policy_is_refused_before_a_client_exists(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    code = local(world).run(
        "--set", "copilot_eval_v2", "--case-policy", "copilot_eval_v3_case_policy_v2"
    )
    assert code == 2
    assert world.built == []
    assert not world.out.exists()
    assert "applies to copilot_eval_v3 only" in capsys.readouterr().err


# --- R2.10A: the diagnostics block is asked for here, for the knowledge set only ------------------


def test_a_knowledge_run_carries_the_diagnostics_block_in_its_report_only(world: World) -> None:
    case = COPILOT_KNOWLEDGE_EVAL_V1.cases[0].case_id
    assert local(world).run("--set", "copilot_knowledge_eval_v1", "--case", case) == 0

    replays, report = world.files()
    assert "knowledge_diagnostics" not in replays
    block = report["knowledge_diagnostics"]
    assert block["version"] == "copilot_knowledge_diagnostics_v2"
    assert block["correctness"]["version"] == "copilot_knowledge_correctness_v1"
    [row] = block["cases"]
    assert (row["case_id"], row["model_text"]) == (case, world.text)
    assert row["correctness"]["model"]["answer_correctness"] == "incorrect"  # world.text


@pytest.mark.parametrize("question_set", ["copilot_eval_v1", "copilot_eval_v2", "copilot_eval_v3"])
def test_no_other_set_carries_the_diagnostics_block(world: World, question_set: str) -> None:
    assert local(world).run("--set", question_set, "--case", "kpis_may_occupancy") == 0

    _, report = world.files()
    assert "knowledge_diagnostics" not in report


def test_a_repeated_knowledge_run_records_the_block_in_every_attempt(world: World) -> None:
    case = COPILOT_KNOWLEDGE_EVAL_V1.cases[0].case_id
    code = local(world).run("--set", "copilot_knowledge_eval_v1", "--case", case, "--repeat", "2")
    assert code == 0
    for attempt in (1, 2):
        path = world.out / f"report-{TODAY}-attempt-{attempt}.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        assert [row["case_id"] for row in report["knowledge_diagnostics"]["cases"]] == [case]
