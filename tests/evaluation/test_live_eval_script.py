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
    # R2.8E: v4 is now an allowed choice (below); v5 stands for an unknown version instead.
    "prompt",
    ["copilot_answer@v1", "copilot_answer@v5", "v3", "v4", "boundary_probe@v1"],
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
        "copilot_answer@v5",
        "copilot_conversation@v1",
        "boundary_probe@v1",
    ):
        with pytest.raises(script.RunRefusedError):
            script.resolve_prompt(identity)


# --- R2.8E: copilot_answer@v4, an evaluation-only choice -----------------------------------------

V4 = "-copilot_answer-v4"


def test_v4_is_allowed_only_when_named_and_the_default_stays_v2() -> None:
    from app.llm.prompts.registry import COPILOT_ANSWER_V4

    script = load_script()
    assert script.PROMPTS == ("copilot_answer@v2", "copilot_answer@v3", "copilot_answer@v4")
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
