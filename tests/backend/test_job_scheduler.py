"""R4.2: the retention scheduler, offline -- what it runs, in what order, and how it fails.

The jobs themselves are exercised against PostgreSQL by their own integration tests (the archival
command by tests/integration/test_audit_archive_job.py); here each is a stand-in that records its
call, so the scheduler's own behaviour is all that is under test.
"""

from __future__ import annotations

import pkgutil
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

import app.jobs
from app.core.config import Settings
from app.jobs import scheduler
from app.jobs.scheduler import DEFAULT_INTERVAL_HOURS, JOBS, Job, main, run_once

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = REPOSITORY_ROOT / "docker-compose.yml"
SCHEDULER_COMPOSE = REPOSITORY_ROOT / "docker-compose.scheduler.yml"
SETTINGS = Settings(environment="test", secret_key="scheduler-test-secret")
DAY_SECONDS = DEFAULT_INTERVAL_HOURS * 3600


def no_sleep(seconds: float) -> None:
    """A sleep that returns at once, for the tests that must never reach one."""


# Every module in app.jobs is either scheduled or, with its reason, not.
NOT_SCHEDULED = {
    "scheduler": "the scheduler itself",
    "demand_observation": "declares an observation period on an operator's say-so; not retention",
    "platform_admin": "grants or revokes platform administration on an operator's say-so",
}


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str] | None, Settings | None]] = []

    def job(self, name: str, status: int = 0, raises: bool = False) -> Job:
        def run(argv: list[str] | None = None, *, settings: Settings | None = None) -> int:
            self.calls.append((name, argv, settings))
            if raises:
                raise RuntimeError("postgresql://user:SECRET@host/db")
            return status

        return Job(name, run)

    @property
    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]


def test_it_schedules_the_three_purges_and_the_archival() -> None:
    assert [job.run.__module__ for job in JOBS] == [
        "app.jobs.archive_audit_events",
        "app.jobs.purge_conversations",
        "app.jobs.purge_llm_invocations",
        "app.jobs.purge_demand_predictions",
    ]
    assert all(job.run.__name__ == "main" for job in JOBS)


def test_every_job_module_is_scheduled_or_excluded_by_reason() -> None:
    modules = {m.name for m in pkgutil.iter_modules(app.jobs.__path__)}
    scheduled = {job.run.__module__.rsplit(".", 1)[1] for job in JOBS}
    assert modules == scheduled | set(NOT_SCHEDULED)
    assert not scheduled & set(NOT_SCHEDULED)


def test_once_runs_every_job_in_order_with_the_settings_and_no_arguments(
    capsys: pytest.CaptureFixture[str],
) -> None:
    recorder = Recorder()
    jobs = [recorder.job("a"), recorder.job("b"), recorder.job("c")]

    assert main(["--once"], settings=SETTINGS, jobs=jobs) == 0

    assert recorder.calls == [("a", [], SETTINGS), ("b", [], SETTINGS), ("c", [], SETTINGS)]
    assert capsys.readouterr().out.strip().endswith("scheduler: 3 of 3 job(s) succeeded")


def test_a_failed_job_does_not_stop_the_others_and_fails_the_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    recorder = Recorder()
    jobs = [recorder.job("a"), recorder.job("b", status=1), recorder.job("c")]

    assert run_once(SETTINGS, jobs) == 1

    assert recorder.names == ["a", "b", "c"]
    assert "scheduler: 2 of 3 job(s) succeeded; failed: b" in capsys.readouterr().out


def test_once_fails_when_a_job_fails() -> None:
    recorder = Recorder()
    assert main(["--once"], settings=SETTINGS, jobs=[recorder.job("a", status=1)]) == 1


def test_a_job_that_raises_is_reported_by_type_only(capsys: pytest.CaptureFixture[str]) -> None:
    recorder = Recorder()
    jobs = [recorder.job("a", raises=True), recorder.job("b")]

    assert run_once(SETTINGS, jobs) == 1

    captured = capsys.readouterr()
    assert recorder.names == ["a", "b"]
    assert "a FAILED (RuntimeError)" in captured.err
    assert "SECRET" not in captured.err + captured.out


def test_the_loop_repeats_on_the_interval_from_each_cycles_start() -> None:
    recorder = Recorder()
    slept: list[float] = []

    status = main([], settings=SETTINGS, jobs=[recorder.job("a")], sleep=slept.append, max_cycles=3)

    assert status == 0
    assert recorder.names == ["a", "a", "a"]
    assert len(slept) == 2  # between cycles, not after the last
    assert all(DAY_SECONDS - 60 < s <= DAY_SECONDS for s in slept)


def test_a_failing_cycle_does_not_stop_the_schedule() -> None:
    recorder = Recorder()
    slept: list[float] = []

    main(
        [], settings=SETTINGS, jobs=[recorder.job("a", status=1)], sleep=slept.append, max_cycles=2
    )

    assert recorder.names == ["a", "a"] and len(slept) == 1


def test_the_interval_is_configurable() -> None:
    slept: list[float] = []
    main(
        ["--interval-hours", "0.5"],
        settings=SETTINGS,
        jobs=[Recorder().job("a")],
        sleep=slept.append,
        max_cycles=2,
    )
    assert len(slept) == 1 and 1740 < slept[0] <= 1800


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "daily", "0.0001"])
def test_an_invalid_interval_is_a_usage_error(value: str) -> None:
    recorder = Recorder()
    # Bounded, so an interval wrongly accepted fails this test rather than looping forever.
    with pytest.raises(SystemExit) as exited:
        main(
            ["--interval-hours", value],
            settings=SETTINGS,
            jobs=[recorder.job("a")],
            sleep=no_sleep,
            max_cycles=2,
        )
    assert exited.value.code == 2
    assert recorder.calls == []


def test_an_unknown_argument_is_a_usage_error() -> None:
    recorder = Recorder()
    with pytest.raises(SystemExit) as exited:
        main(
            ["--hotel", "x"],
            settings=SETTINGS,
            jobs=[recorder.job("a")],
            sleep=no_sleep,
            max_cycles=2,
        )
    assert exited.value.code == 2
    assert recorder.calls == []


def test_a_configuration_error_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse() -> Settings:
        raise ValueError("postgresql://user:SECRET@host/db is not allowed")

    monkeypatch.setattr(scheduler, "get_settings", refuse)
    recorder = Recorder()

    assert main(["--once"], jobs=[recorder.job("a")]) == 1

    captured = capsys.readouterr()
    assert recorder.calls == []
    assert "scheduler FAILED (ValueError)" in captured.err
    assert "SECRET" not in captured.err + captured.out


# --- the opt-in compose service ---------------------------------------------------------------


def compose(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_the_base_stack_is_unchanged_and_has_no_scheduler() -> None:
    assert set(compose(BASE_COMPOSE)["services"]) == {"db", "migrate", "api", "frontend"}


def test_the_override_adds_only_the_scheduler() -> None:
    assert set(compose(SCHEDULER_COMPOSE)["services"]) == {"scheduler"}


def test_the_scheduler_is_the_api_image_with_the_api_configuration() -> None:
    service = compose(SCHEDULER_COMPOSE)["services"]["scheduler"]
    assert service["extends"] == {"file": "docker-compose.yml", "service": "api"}
    assert "api" in compose(BASE_COMPOSE)["services"]


def test_the_scheduler_waits_for_the_migration() -> None:
    service = compose(SCHEDULER_COMPOSE)["services"]["scheduler"]
    assert service["depends_on"] == {"migrate": {"condition": "service_completed_successfully"}}


def test_the_scheduler_runs_the_scheduler_and_publishes_nothing() -> None:
    service = compose(SCHEDULER_COMPOSE)["services"]["scheduler"]
    assert service["command"] == ["python", "-m", "app.jobs.scheduler"]
    assert service["healthcheck"] == {"disable": True}
    assert "ports" not in service and "environment" not in service
