"""R0.2: the CI jobs that pull Docker Hub images in their steps try a pull-through mirror first.

Read from the workflow file itself, as the other CI rules are, so the step cannot be dropped or
moved after the first pull without a test failing. What the mirror can and cannot change is
argued in the step's own comment; these tests hold the three facts that argument rests on: it
is the daemon's ``registry-mirrors`` (so Docker Hub stays the fallback), it runs before the job
pulls or builds anything, and it checks that it took effect.

The workflow is split on its own indentation -- two spaces for a job, six for a step -- rather
than parsed: no YAML library is a declared dependency of the test suite.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
MIRROR = "https://mirror.gcr.io"
STEP = "Pull Docker Hub images through a mirror, Docker Hub the fallback"
#: The jobs whose steps pull or build images. The others pull only `services:` containers,
#: which the runner fetches before any step can configure anything.
MIRRORED_JOBS = ("docker-runtime", "image-reproducibility", "responsive-audit")

JOB = re.compile(r"^  ([a-z][a-z0-9-]*):\n", re.MULTILINE)
STEP_START = re.compile(r"^      - name: (.+)$", re.MULTILINE)


def jobs() -> dict[str, str]:
    """job id -> its text, from `jobs:` to the next job."""
    text = WORKFLOW.read_text(encoding="utf-8")
    body = text[text.index("\njobs:\n") :]
    starts = list(JOB.finditer(body))
    return {
        match.group(1): body[match.end() : (starts[i + 1].start() if i + 1 < len(starts) else None)]
        for i, match in enumerate(starts)
    }


def steps_of(job: str) -> list[tuple[str, str]]:
    """(name, text) of each named step of *job*, in order."""
    text = jobs()[job]
    starts = list(STEP_START.finditer(text))
    return [
        (
            match.group(1).strip(),
            text[match.start() : (starts[i + 1].start() if i + 1 < len(starts) else None)],
        )
        for i, match in enumerate(starts)
    ]


def mirror_step(job: str) -> str:
    (text,) = [text for name, text in steps_of(job) if name == STEP]
    return text


@pytest.mark.parametrize("job", MIRRORED_JOBS)
def test_the_job_configures_the_mirror_right_after_checkout(job: str) -> None:
    names = [name for name, _ in steps_of(job)]

    assert names[0] == "Check out the repository"
    assert names[1] == STEP


@pytest.mark.parametrize("job", MIRRORED_JOBS)
def test_no_step_before_it_touches_docker(job: str) -> None:
    (_, checkout) = steps_of(job)[0]

    assert "docker" not in checkout


@pytest.mark.parametrize("job", MIRRORED_JOBS)
def test_it_is_the_daemons_registry_mirror_so_docker_hub_stays_the_fallback(job: str) -> None:
    script = mirror_step(job)

    assert '"registry-mirrors": ["' + MIRROR + '"]' in script
    assert "/etc/docker/daemon.json" in script
    # Merged into whatever the runner already configures, never replacing it.
    assert "jq '. + {" in script
    assert "sudo systemctl restart docker" in script


@pytest.mark.parametrize("job", MIRRORED_JOBS)
def test_it_fails_the_job_unless_the_mirror_took_effect(job: str) -> None:
    script = mirror_step(job)

    assert "set -euo pipefail" in script
    assert "docker info --format '{{json .RegistryConfig.Mirrors}}'" in script
    assert 'echo "::error::the registry mirror is not active"; exit 1' in script


def test_only_those_jobs_change_the_daemon() -> None:
    """Every other job's images are `services:`, pulled before a step could configure a mirror."""
    configuring = {name for name, text in jobs().items() if "registry-mirrors" in text}

    assert configuring == set(MIRRORED_JOBS)
