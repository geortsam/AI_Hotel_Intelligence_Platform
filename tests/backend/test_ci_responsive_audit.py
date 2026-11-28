"""A9: CI runs UI-10's 51-check responsive audit against the real stack on every workflow run.

The workflow half of the guarantee; the rule half is frontend/e2e/responsive-checks.test.mjs
(Vitest), which holds that each of the three conditions fails a check, that the measurement's
clipping rule and tolerance are UI-10's, and that a short or failing run is never a pass.

Here: the job exists and cannot be skipped or excused; it builds and starts the Compose stack,
seeds it with the synthetic demo data and grants the platform role; it drives the browser from
an image pinned by digest whose Playwright version is the one the lockfile installs; the
credentials it generates are masked; the report is uploaded with a pinned action; and the
specification the runner audits is the one docs/ui-redesign.md states -- 17 routes, each a route
the application has, at 375, 768 and 1440px. Read from the files, split on indentation (no YAML
library is a declared test dependency).

Since 2026-11-27 the runner also fails the run on its five table checks, the Analytics breakdown
tables at 375, 768, 1024, 1280 and 1440px, which replaced the report-only probe of the
expenses-table clipping when that defect was fixed. The workflow is unchanged: its step still
runs the same runner.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
CHECKS = ROOT / "frontend" / "e2e" / "responsive-checks.mjs"
RUNNER = ROOT / "frontend" / "e2e" / "responsive-audit.mjs"
JOB_ID = "responsive-audit"
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2"
STEPS = [
    "Check out the repository",
    "Pull Docker Hub images through a mirror, Docker Hub the fallback",
    "Set up Node.js",
    "Install the frontend dependencies",
    "Prepare the disposable environment",
    "Build and start the stack",
    "Seed the synthetic demo data",
    "Run the 51-check responsive audit",
    "Upload the audit report",
    "Tear down the disposable environment",
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


def job() -> str:
    found = jobs()
    assert JOB_ID in found, "the responsive audit job is missing from ci.yml"
    return found[JOB_ID]


def steps() -> dict[str, str]:
    text = job()
    starts = list(STEP_START.finditer(text))
    return {
        match.group(1).strip(): text[
            match.start() : (starts[i + 1].start() if i + 1 < len(starts) else None)
        ]
        for i, match in enumerate(starts)
    }


def code(step: str) -> str:
    return "\n".join(
        line for line in steps()[step].splitlines() if not line.strip().startswith("#")
    )


def env_value(name: str) -> str:
    match = re.search(rf"^      {name}: (.+)$", job(), re.MULTILINE)
    assert match, name
    return match.group(1).strip().strip("'")


# --- the job ------------------------------------------------------------------------------------


def test_the_responsive_audit_job_exists_with_its_steps_in_order() -> None:
    assert "    name: Responsive audit\n" in job()
    assert list(steps()) == STEPS


def test_nothing_can_skip_or_excuse_the_audit() -> None:
    text = job()
    assert not re.search(r"^    (if|needs|continue-on-error):", text, re.MULTILINE)
    for name in STEPS[:-2]:
        assert not re.search(r"^        (if|continue-on-error):", steps()[name], re.M), name
    # Only the report upload and the teardown run after a failure, and neither can pass a run.
    for name in STEPS[-2:]:
        assert "        if: always()\n" in steps()[name], name
        assert "continue-on-error" not in steps()[name], name
    workflow = WORKFLOW.read_text(encoding="utf-8")
    on = workflow[workflow.index("\non:\n") : workflow.index("\nconcurrency:")]
    assert "\n  push:\n" in on and "\n  pull_request:\n" in on


def test_the_audit_runs_the_runner_in_the_pinned_browser_against_the_stack() -> None:
    run = code("Run the 51-check responsive audit")
    assert '"$PLAYWRIGHT_IMAGE" node e2e/responsive-audit.mjs' in run
    assert '-e AUDIT_BASE_URL="https://127.0.0.1:${HTTPS_PORT}"' in run
    assert "-e AUDIT_REPORT=/out/responsive-audit.json" in run
    assert '-v "$PWD/frontend:/work:ro"' in run
    # The password is passed by name from the step's environment, never on the command line.
    assert 'AUDIT_PASSWORD="$OWNER_PASSWORD" docker run' in run
    assert "-e AUDIT_PASSWORD \\" in run
    # A local override exists for running outside the image; CI never uses it.
    assert "AUDIT_CHROMIUM" not in job()


def test_the_browser_image_is_pinned_by_digest_at_the_lockfiles_playwright() -> None:
    image = env_value("PLAYWRIGHT_IMAGE")
    match = re.fullmatch(
        r"mcr\.microsoft\.com/playwright:v(?P<version>[0-9.]+)-noble@sha256:(?P<digest>[0-9a-f]{64})",
        image,
    )
    assert match, image
    assert match.group("digest") == (
        "9beb546099e11f52d62e505537914870e01a67e0e6b00ae41da4775324e73dbe"
    )
    package = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
    assert package["devDependencies"]["playwright-core"] == match.group("version") == "1.65.0"
    assert lock["packages"]["node_modules/playwright-core"]["version"] == "1.65.0"
    assert lock["packages"]["node_modules/playwright-core"]["dev"] is True


def test_the_stack_is_built_from_this_commit_and_awaited() -> None:
    start = code("Build and start the stack")
    assert "dc up -d --build frontend" in start
    assert "{{.State.Health.Status}}" in start
    assert '[ "$state" = "healthy" ] && break' in start
    assert "exit 1" in start


def test_the_stack_is_seeded_and_the_audit_user_holds_the_platform_role() -> None:
    seed = code("Seed the synthetic demo data")
    assert "python scripts/seed_demo.py" in seed
    assert '--reference-date today --owner-email "$DEMO_OWNER_EMAIL"' in seed
    assert '-v "$PWD/scripts:/app/scripts:ro" migrate' in seed
    assert 'python -m app.jobs.platform_admin grant "$DEMO_OWNER_EMAIL"' in seed
    assert env_value("DEMO_OWNER_EMAIL") == "demo.owner@example.com"
    names = list(steps())
    assert names.index("Seed the synthetic demo data") < names.index(
        "Run the 51-check responsive audit"
    )


def test_every_generated_credential_is_masked_and_no_secret_is_used() -> None:
    prepare = code("Prepare the disposable environment")
    for name in ("PG_PASSWORD", "APP_SECRET", "OWNER_PASSWORD"):
        assert f'echo "::add-mask::${name}"' in prepare, name
        assert f'{name}="$(openssl rand' in prepare, name
    text = job()
    assert "secrets." not in text
    assert "github.token" not in text and "GITHUB_TOKEN" not in text
    assert "persist-credentials: false" in steps()["Check out the repository"]
    # The key and the env file are removed with the stack.
    assert 'rm -rf "$CERT_DIR" "$ENV_FILE"' in steps()["Tear down the disposable environment"]
    assert "down -v --remove-orphans" in steps()["Tear down the disposable environment"]


def test_the_report_is_uploaded_with_the_pinned_action() -> None:
    upload = steps()["Upload the audit report"]
    assert f"uses: {UPLOAD_ACTION}" in upload
    assert "name: responsive-audit-${{ github.run_id }}-${{ github.run_attempt }}" in upload
    assert "path: ${{ runner.temp }}/audit/" in upload
    assert "if-no-files-found: error" in upload


# --- the specification the runner audits -------------------------------------------------------


def specification() -> tuple[list[str], list[int], int]:
    text = CHECKS.read_text(encoding="utf-8")
    routes_block = re.search(r"export const ROUTES = Object\.freeze\(\[(.*?)\]\)", text, re.S)
    widths = re.search(r"export const WIDTHS = Object\.freeze\(\[([0-9, ]+)\]\)", text)
    count = re.search(r"export const CHECK_COUNT = (\d+)", text)
    assert routes_block and widths and count
    routes = re.findall(r"'([^']+)'", routes_block.group(1))
    return routes, [int(w) for w in widths.group(1).split(",")], int(count.group(1))


def test_the_runner_audits_ui_10s_17_routes_at_its_three_widths() -> None:
    routes, widths, count = specification()
    assert len(routes) == 17 == len(set(routes))
    assert widths == [375, 768, 1440]
    assert count == 51 == len(routes) * len(widths)
    docs = (ROOT / "docs" / "ui-redesign.md").read_text(encoding="utf-8")
    assert "17 routes \u00d7 375, 768 and 1440px" in docs


def test_every_audited_route_is_one_the_application_has() -> None:
    routes, _, _ = specification()
    declared = set(
        re.findall(r":\s*'(/[a-z/]*)'", (ROOT / "frontend/src/router/routes.ts").read_text("utf-8"))
    )
    for route in routes:
        if route == "/nowhere":
            assert route not in declared
            continue
        static = "/" + route.split("/")[1] if route != "/" else "/"
        assert static in declared, route


def test_the_runner_signs_in_and_fails_unless_every_check_passed() -> None:
    text = RUNNER.read_text(encoding="utf-8")
    assert "await signIn(context)" in text
    assert "plan({ booking, guest, roomType, roomNumber })" in text
    assert "return 1" in text
    assert "process.exit(exitCode)" in text
    # The five table checks (the 2026-11-27 fix of the expenses-table clipping) fail the run
    # too, and a scrolling table is scrolled by keyboard, not just measured.
    assert "if (!verdict.ok || !tableVerdict.ok) {" in text
    assert "for (const width of TABLE_WIDTHS) {" in text
    assert "keyboard[name] = await scrollByKeyboard(page, name)" in text
    assert "evaluateTables(width, m, keyboard)" in text
    assert "KNOWN_DEFECT" not in text
    # The password is read from the environment and never logged.
    assert "required('AUDIT_PASSWORD')" in text
    assert not re.search(r"console\.\w+\([^)]*password", text)
