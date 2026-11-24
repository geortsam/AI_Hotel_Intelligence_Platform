"""A3: CI generates one CycloneDX SBOM per image, from the built images, and a wrong one fails.

Two halves, as in test_ci_dependency_audit.py. The workflow half reads the image-reproducibility
job of .github/workflows/ci.yml, split on its own indentation (no YAML library is a declared test
dependency), and holds what the SBOM steps run, in what order, with what pins. The behaviour half
drives scripts/sbom_check.py on documents built here -- no Docker, no Syft, and a stand-in for the
CycloneDX schema validator, which lives in the checker's own lock rather than in this environment
-- and holds every decision it makes: a missing, malformed, foreign or schema-invalid SBOM fails;
a locked package that is missing or at another version fails; two builds that differ fail; a
host path, a credential's value or a credential's shape fails.

The live half -- that Syft 1.57.1 catalogues the real images and that the real schema validator
rejects the control fixture -- is the job itself, on every workflow run.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest

from scripts import dependency_audit, sbom_check

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
JOB_ID = "image-reproducibility"
SCRIPT = "scripts/sbom_check.py"
LOCK = ROOT / "backend" / "requirements-lock.txt"
SBOM_LOCK = ROOT / "scripts" / "sbom_lock.txt"
CONTROL = "tests/fixtures/sbom/invalid-component-type.cdx.json"
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2"
SBOM_STEPS = [
    "Set up Python for the SBOM checker",
    "Install Syft and the SBOM checker",
    "Generate an SBOM for each image, from both builds",
    "Prove the SBOM checker rejects an invalid SBOM",
    "Validate the SBOMs",
    "Upload the SBOMs",
]

JOB = re.compile(r"^  ([a-z][a-z0-9-]*):\n", re.MULTILINE)
STEP_START = re.compile(r"^      - name: (.+)$", re.MULTILINE)


def jobs() -> dict[str, str]:
    text = WORKFLOW.read_text(encoding="utf-8")
    body = text[text.index("\njobs:\n") :]
    starts = list(JOB.finditer(body))
    return {
        match.group(1): body[match.end() : (starts[i + 1].start() if i + 1 < len(starts) else None)]
        for i, match in enumerate(starts)
    }


def steps() -> dict[str, str]:
    text = jobs()[JOB_ID]
    starts = list(STEP_START.finditer(text))
    return {
        match.group(1).strip(): text[
            match.start() : (starts[i + 1].start() if i + 1 < len(starts) else None)
        ]
        for i, match in enumerate(starts)
    }


def code(step: str) -> str:
    """A step's text with its comment lines dropped."""
    return "\n".join(
        line for line in steps()[step].splitlines() if not line.strip().startswith("#")
    )


# --- the workflow -----------------------------------------------------------------------------


def test_the_sbom_steps_run_in_order_after_both_images_are_built() -> None:
    names = list(steps())
    found = [name for name in names if name in SBOM_STEPS]
    assert found == SBOM_STEPS
    first = names.index(SBOM_STEPS[0])
    assert names.index("Build the backend image twice, from scratch") < first
    assert names.index("Build the frontend image twice, from scratch") < first
    assert names.index("Verify the images ship the versions the pins claim") < first
    # The images are removed by the job's last, always() step: the SBOMs come before it.
    assert names[-1] == "Remove the experiment's images"
    assert names.index(SBOM_STEPS[-1]) < len(names) - 1


def test_the_sbom_steps_cannot_be_skipped_and_get_no_secret() -> None:
    job = jobs()[JOB_ID]
    assert not re.search(r"^    (if|needs|continue-on-error):", job, re.MULTILINE)
    for name in SBOM_STEPS:
        text = steps()[name]
        assert not re.search(r"^        (if|continue-on-error):", text, re.MULTILINE), name
        assert "secrets." not in text, name
        assert "GITHUB_TOKEN" not in text and "github.token" not in text, name
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "\npermissions:\n  contents: read\n" in text
    on = text[text.index("\non:\n") : text.index("\nconcurrency:")]
    assert "\n  push:\n" in on and "\n  pull_request:\n" in on


def test_syft_is_pinned_by_version_and_sha256_and_checked_before_it_is_opened() -> None:
    install = code("Install Syft and the SBOM checker")
    version = re.search(r"SYFT_VERSION: '([0-9.]+)'", install)
    digest = re.search(r"SYFT_SHA256: '([0-9a-f]+)'", install)
    assert version and digest
    assert version.group(1) == sbom_check.SYFT_VERSION == "1.57.1"
    assert digest.group(1) == "bf52519eb57f675d47c07d43213cb6dff6449f3d6d1147ff6701ed91f44eef0c"
    check = install.index('echo "${SYFT_SHA256}  ${archive}" | sha256sum --check --strict -')
    assert check < install.index("tar -xzf")
    assert "https://github.com/anchore/syft/releases/download/v${SYFT_VERSION}/" in install
    # The binary must say it is the pinned version, and nothing is piped into a shell.
    assert '[ "$got" = "$SYFT_VERSION" ]' in install
    assert not re.search(r"\|\s*(sudo\s+)?(ba|z)?sh\b", install)


def test_the_checker_is_installed_from_its_hashed_lock_into_its_own_venv() -> None:
    install = code("Install Syft and the SBOM checker")
    assert 'python -m venv "$RUNNER_TEMP/sbom-check"' in install
    assert "-m pip install --no-deps --require-hashes -r scripts/sbom_lock.txt" in install
    assert not re.search(r"^\s+cache:", steps()["Set up Python for the SBOM checker"], re.M)
    entries = dependency_audit.parse_lock(SBOM_LOCK.read_text(encoding="utf-8"))
    pins = {entry.name: entry.version for entry in entries}
    assert pins["cyclonedx-python-lib"] == "11.12.0"
    assert pins["jsonschema"] and pins["packaging"] == "26.3"
    assert all(entry.hashes for entry in entries)


def test_both_images_and_both_builds_are_catalogued_from_the_daemon() -> None:
    generate = code("Generate an SBOM for each image, from both builds")
    specs = re.search(r"for spec in (.+); do", generate)
    assert specs
    assert re.findall(r'"([^"]+)"', specs.group(1)) == [
        "$TAG_A backend a",
        "$TAG_B backend b",
        "$FE_TAG_A frontend a",
        "$FE_TAG_B frontend b",
    ]
    # The built image, from the Docker daemon -- not a directory, not a lockfile.
    assert '"$RUNNER_TEMP/syft/syft" scan "docker:$tag"' in generate
    assert '-o "cyclonedx-json@1.6=$out/$image-$slot.cdx.json"' in generate
    assert sbom_check.SPEC_VERSION == "1.6"


def test_the_positive_control_runs_before_the_real_sboms_are_checked() -> None:
    names = list(steps())
    control = code("Prove the SBOM checker rejects an invalid SBOM")
    assert f"{SCRIPT} expect-invalid {CONTROL}" in control
    assert names.index("Prove the SBOM checker rejects an invalid SBOM") < names.index(
        "Validate the SBOMs"
    )
    document = json.loads((ROOT / CONTROL).read_text(encoding="utf-8"))
    assert [c["type"] for c in document["components"]] == ["not-a-component-type"]


def test_every_check_runs_on_the_real_sboms() -> None:
    validate = code("Validate the SBOMs")
    out = '"$out/'
    assert (
        f'check validate {out}backend-a.cdx.json" {out}backend-b.cdx.json" '
        f'{out}frontend-a.cdx.json" {out}frontend-b.cdx.json"'
    ) in validate
    assert f'check backend {out}backend-a.cdx.json" backend/requirements-lock.txt' in validate
    assert f'check same {out}backend-a.cdx.json" {out}backend-b.cdx.json"' in validate
    assert f'check same {out}frontend-a.cdx.json" {out}frontend-b.cdx.json"' in validate
    assert 'check clean "$out"/*.cdx.json' in validate
    # The nginx version is the one the image is asserted to ship, a step earlier.
    nginx = re.search(r'check frontend "\$out/frontend-a\.cdx\.json" ([0-9.]+)', validate)
    assert nginx
    pins = steps()["Verify the images ship the versions the pins claim"]
    assert f'[ "$got" = "{nginx.group(1)}" ]' in pins


def test_one_validated_sbom_per_image_is_uploaded_and_a_missing_one_fails() -> None:
    upload = steps()["Upload the SBOMs"]
    assert f"uses: {UPLOAD_ACTION}" in upload
    assert "name: sbom-${{ github.run_id }}-${{ github.run_attempt }}" in upload
    assert "path: ${{ runner.temp }}/sbom/" in upload
    assert "if-no-files-found: error" in upload
    validate = code("Validate the SBOMs")
    assert 'mv "$out/backend-a.cdx.json" "$out/backend.cdx.json"' in validate
    assert 'mv "$out/frontend-a.cdx.json" "$out/frontend.cdx.json"' in validate


# --- the checker's decisions ------------------------------------------------------------------

SYFT = {"type": "application", "author": "anchore", "name": "syft", "version": "1.57.1"}


def bom(
    components: Iterable[Mapping[str, Any]],
    *,
    tools: Iterable[Mapping[str, Any]] = (SYFT,),
    serial: str = "urn:uuid:00000000-0000-4000-8000-000000000001",
    timestamp: str = "2026-11-24T00:00:00Z",
) -> dict[str, Any]:
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": serial,
        "version": 1,
        "metadata": {"timestamp": timestamp, "tools": {"components": list(tools)}},
        "components": list(components),
    }


def pypi(name: str, version: str) -> dict[str, str]:
    return {"type": "library", "name": name, "version": version,
            "purl": f"pkg:pypi/{name}@{version}"}  # fmt: skip


DEBIAN = {"type": "operating-system", "name": "debian", "version": "13"}
ALPINE = {"type": "operating-system", "name": "alpine", "version": "3.21.3"}
NGINX = {"type": "library", "name": "nginx", "version": "1.27.5-r1",
         "purl": "pkg:apk/alpine/nginx@1.27.5-r1?arch=x86_64"}  # fmt: skip


def write(tmp_path: Path, name: str, document: Mapping[str, Any]) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def no_schema_error(_: str) -> str | None:
    return None


def locked_for_the_image() -> list[tuple[str, str]]:
    return [
        (name, version)
        for name, version, marker in sbom_check.lock_entries(LOCK.read_text(encoding="utf-8"))
        if sbom_check.image_installs(marker)
    ]


def backend_bom(drop: str = "", replace: tuple[str, str] | None = None) -> dict[str, Any]:
    listed = []
    for name, version in locked_for_the_image():
        if name == drop:
            continue
        if replace and name == replace[0]:
            version = replace[1]
        listed.append(pypi(name, version))
    return bom([DEBIAN, *listed, pypi("pip", "25.3")])


def test_a_well_formed_sbom_validates(tmp_path: Path) -> None:
    path = write(tmp_path, "ok.json", bom([DEBIAN, pypi("fastapi", "0.141.1")]))
    assert sbom_check.validate(path, schema=no_schema_error) == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [("bomFormat", "SPDX"), ("specVersion", "1.5"), ("components", [])],
)
def test_a_document_that_is_not_a_cyclonedx_16_sbom_fails(
    tmp_path: Path, field: str, value: object
) -> None:
    document = bom([DEBIAN])
    document[field] = value
    with pytest.raises(sbom_check.CheckFailedError):
        sbom_check.validate(write(tmp_path, "x.json", document), schema=no_schema_error)


def test_a_schema_error_fails_validation(tmp_path: Path) -> None:
    path = write(tmp_path, "x.json", bom([DEBIAN]))
    with pytest.raises(sbom_check.CheckFailedError, match=r"invalid against the CycloneDX 1\.6"):
        sbom_check.validate(path, schema=lambda _: "'nope' is not one of [...]")


def test_an_sbom_from_another_generator_fails(tmp_path: Path) -> None:
    for tools in ([], [{**SYFT, "version": "1.56.0"}], [{**SYFT, "name": "trivy"}]):
        path = write(tmp_path, "x.json", bom([DEBIAN], tools=tools))
        with pytest.raises(sbom_check.CheckFailedError, match=r"not written by Syft 1\.57\.1"):
            sbom_check.validate(path, schema=no_schema_error)


def test_a_missing_or_unreadable_sbom_fails(tmp_path: Path) -> None:
    with pytest.raises(sbom_check.CheckFailedError, match="no SBOM was written"):
        sbom_check.validate(tmp_path / "absent.json", schema=no_schema_error)
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(sbom_check.CheckFailedError, match="not JSON"):
        sbom_check.validate(broken, schema=no_schema_error)


def test_the_control_fixture_fails_only_on_its_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    control = ROOT / CONTROL
    # With every non-schema check satisfied, the schema is what must reject it...
    monkeypatch.setattr(
        sbom_check,
        "schema_errors",
        lambda text: "bad type" if "not-a-component-type" in text else None,
    )
    assert sbom_check.main(["sbom_check.py", "expect-invalid", str(control)]) == 0
    # ...and a validator that accepts anything fails the control step.
    monkeypatch.setattr(sbom_check, "schema_errors", no_schema_error)
    assert sbom_check.main(["sbom_check.py", "expect-invalid", str(control)]) == 1


def test_every_locked_package_at_its_version_passes(tmp_path: Path) -> None:
    expected, listed = sbom_check.check_backend(write(tmp_path, "b.json", backend_bom()), LOCK)
    assert expected == 39
    assert listed == 41


@pytest.mark.parametrize("package", ["pyjwt", "fastapi", "psycopg-binary", "uvloop"])
def test_a_package_missing_from_the_image_fails(tmp_path: Path, package: str) -> None:
    path = write(tmp_path, "b.json", backend_bom(drop=package))
    with pytest.raises(sbom_check.CheckFailedError, match=f"{package}=="):
        sbom_check.check_backend(path, LOCK)


def test_a_package_at_another_version_fails(tmp_path: Path) -> None:
    path = write(tmp_path, "b.json", backend_bom(replace=("pyjwt", "2.13.0")))
    with pytest.raises(sbom_check.CheckFailedError, match=r"pyjwt==2\.15\.1"):
        sbom_check.check_backend(path, LOCK)


def test_names_are_compared_as_pypi_normalises_them(tmp_path: Path) -> None:
    listed = [
        pypi(name.upper().replace("-", "_"), version) for name, version in locked_for_the_image()
    ]
    sbom_check.check_backend(write(tmp_path, "b.json", bom([DEBIAN, *listed])), LOCK)


def test_markers_are_read_for_the_image_not_for_this_machine() -> None:
    markers = {name: marker for name, _, marker in sbom_check.lock_entries(LOCK.read_text("utf-8"))}
    assert not sbom_check.image_installs(markers["tzdata"])
    assert sbom_check.image_installs(markers["uvloop"])
    assert sbom_check.image_installs(markers["greenlet"])
    assert [(n, v) for n, v, _ in sbom_check.lock_entries(LOCK.read_text("utf-8"))] == [
        (e.name, e.version) for e in dependency_audit.parse_lock(LOCK.read_text("utf-8"))
    ]


def test_an_sbom_without_its_operating_system_fails(tmp_path: Path) -> None:
    document = backend_bom()
    document["components"] = [c for c in document["components"] if c is not DEBIAN]
    with pytest.raises(sbom_check.CheckFailedError, match="operating-system"):
        sbom_check.check_backend(write(tmp_path, "b.json", document), LOCK)


def test_the_frontend_sbom_must_list_the_pinned_nginx(tmp_path: Path) -> None:
    assert sbom_check.check_frontend(write(tmp_path, "f.json", bom([ALPINE, NGINX])), "1.27.5") == 2
    other = {**NGINX, "version": "1.27.4-r0"}
    with pytest.raises(sbom_check.CheckFailedError, match=r"no nginx 1\.27\.5"):
        sbom_check.check_frontend(write(tmp_path, "f.json", bom([ALPINE, other])), "1.27.5")
    with pytest.raises(sbom_check.CheckFailedError, match="operating-system"):
        sbom_check.check_frontend(write(tmp_path, "f.json", bom([NGINX])), "1.27.5")


def test_two_builds_listing_the_same_components_agree(tmp_path: Path) -> None:
    a = write(tmp_path, "a.json", bom([ALPINE, NGINX]))
    b = write(
        tmp_path,
        "b.json",
        bom(
            [NGINX, ALPINE],
            serial="urn:uuid:00000000-0000-4000-8000-000000000002",
            timestamp="2026-11-25T00:00:00Z",
        ),
    )
    assert sbom_check.check_same(a, b) == 2


@pytest.mark.parametrize(
    "second",
    [
        [ALPINE],
        [ALPINE, NGINX, NGINX],
        [ALPINE, {**NGINX, "version": "1.27.5-r2"}],
        [ALPINE, NGINX, pypi("extra", "1.0")],
    ],
)
def test_two_builds_that_differ_fail(tmp_path: Path, second: list[dict[str, str]]) -> None:
    a = write(tmp_path, "a.json", bom([ALPINE, NGINX]))
    b = write(tmp_path, "b.json", bom(second))
    with pytest.raises(sbom_check.CheckFailedError, match="differ"):
        sbom_check.check_same(a, b)


def test_a_clean_sbom_passes_the_leak_check(tmp_path: Path) -> None:
    environ = {"RUNNER_TEMP": "/home/runner/work/_temp", "API_TOKEN": "s3cr3t-value-xyz", "CI": "1"}
    sbom_check.check_clean(write(tmp_path, "c.json", bom([ALPINE, NGINX])), environ)


def test_a_host_path_fails_the_leak_check(tmp_path: Path) -> None:
    document = bom([ALPINE, {**NGINX, "properties": [{"name": "x", "value": "/home/runner/x"}]}])
    with pytest.raises(sbom_check.CheckFailedError, match="runner path"):
        sbom_check.check_clean(write(tmp_path, "c.json", document), {})
    document = bom([ALPINE, {**NGINX, "name": "/srv/tmp-7/sbom"}])
    with pytest.raises(sbom_check.CheckFailedError, match=r"\$RUNNER_TEMP"):
        sbom_check.check_clean(write(tmp_path, "c.json", document), {"RUNNER_TEMP": "/srv/tmp-7"})


@pytest.mark.parametrize("name", ["API_TOKEN", "POSTGRES_PASSWORD", "APP_SECRET", "SIGNING_KEY"])
def test_a_credential_value_fails_the_clean_check(tmp_path: Path, name: str) -> None:
    value = "s3cr3t-value-xyz-123"
    document = bom([ALPINE, {**NGINX, "description": f"built with {value}"}])
    with pytest.raises(sbom_check.CheckFailedError) as caught:
        sbom_check.check_clean(write(tmp_path, "c.json", document), {name: value})
    assert f"${name}" in str(caught.value)
    assert value not in str(caught.value)


@pytest.mark.parametrize(
    "text",
    [
        "ghp_" + "a" * 36,
        "-----BEGIN RSA PRIVATE KEY-----",
        "AKIA" + "A" * 16,
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLWJ5dGVz",
    ],
)
def test_a_credential_shape_fails_the_clean_check(tmp_path: Path, text: str) -> None:
    document = bom([ALPINE, {**NGINX, "description": text}])
    with pytest.raises(sbom_check.CheckFailedError):
        sbom_check.check_clean(write(tmp_path, "c.json", document), {})


def test_short_values_are_not_searched_for(tmp_path: Path) -> None:
    sbom_check.check_clean(write(tmp_path, "c.json", bom([ALPINE, NGINX])), {"API_KEY": "1"})


def test_exit_codes(tmp_path: Path) -> None:
    ok = write(tmp_path, "ok.json", bom([ALPINE, NGINX]))
    bad = write(tmp_path, "bad.json", bom([ALPINE]))
    assert sbom_check.main(["sbom_check.py", "same", str(ok), str(ok)]) == 0
    assert sbom_check.main(["sbom_check.py", "same", str(ok), str(bad)]) == 1
    assert sbom_check.main(["sbom_check.py", "unknown"]) == 2
    assert sbom_check.main(["sbom_check.py"]) == 2
