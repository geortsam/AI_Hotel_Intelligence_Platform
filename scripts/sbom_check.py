"""A3: check the CycloneDX SBOMs CI generates for the two images, so a missing or wrong one fails.

CI's image-reproducibility job builds each image twice with ``--no-cache``; Syft (a release binary
pinned by version and SHA-256 in .github/workflows/ci.yml) catalogues each build from the Docker
daemon -- the built image itself, not a lockfile -- into a CycloneDX 1.6 JSON document. This
script then holds every document to five checks, each a subcommand, each exiting 0 when it holds,
1 when it does not and 2 when it cannot run::

    python scripts/sbom_check.py validate SBOM...
    python scripts/sbom_check.py expect-invalid SBOM
    python scripts/sbom_check.py backend SBOM LOCK
    python scripts/sbom_check.py frontend SBOM NGINX_VERSION
    python scripts/sbom_check.py same SBOM_A SBOM_B
    python scripts/sbom_check.py clean SBOM...

``validate``
    The document parses, declares ``bomFormat`` CycloneDX and ``specVersion`` 1.6, validates
    against that schema (cyclonedx-python-lib's strict validator, whose schemas ship inside the
    package, so no network), was written by the pinned Syft, and lists components.
``expect-invalid``
    The positive control: exits 0 only if ``validate`` REJECTS the document. CI runs it on a
    fixture with a broken schema field first, so a validator that accepts anything fails the job.
``backend``
    Every entry of backend/requirements-lock.txt that the image's interpreter installs (its
    environment markers evaluated for CPython 3.14 on Linux x86_64, not for the machine running
    this) is a ``pkg:pypi`` component at exactly the locked version, and the image's operating
    system is recorded.
``frontend``
    The nginx package the image serves with is a component at the pinned version, and the
    image's operating system is recorded. The JavaScript dependencies are not in this image:
    Vite compiles them into the bundle, so no package manifest ships and an image SBOM cannot
    list them. frontend/package-lock.json is what records them, and A2 audits it.
``same``
    Two SBOMs of two independent builds list the same components: the same multiset of
    (type, name, version, purl). Each document's ``serialNumber`` and ``metadata.timestamp``
    differ by design, and per-file locations and layer ids are left out (layer ids are not a
    reproducibility claim this repository makes; docs/deployment/reproducibility.md).
``clean``
    The document carries no host path of the runner, no value of an environment variable whose
    name marks it as a credential, and nothing shaped like a token or a private key. A finding
    is reported by the variable's NAME or the pattern's description, never by the value.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packaging.markers import Marker
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

#: The generator CI pins (.github/workflows/ci.yml); a document written by anything else fails.
SYFT_VERSION = "1.57.1"
SPEC_VERSION = "1.6"

#: The environment the backend image's interpreter evaluates the lock's markers in. Spelled out
#: so the check reads the lock as the image does, wherever this script runs.
IMAGE_ENVIRONMENT: dict[str, str] = {
    "os_name": "posix",
    "sys_platform": "linux",
    "platform_system": "Linux",
    "platform_machine": "x86_64",
    "platform_python_implementation": "CPython",
    "implementation_name": "cpython",
    "python_version": "3.14",
    "python_full_version": "3.14.7",
}

#: Environment variable names whose values are credentials.
SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE|API_KEY|_KEY$")
#: Values shorter than this are not searched for: a two-character value would match anything.
MIN_SECRET_LENGTH = 8
#: Shapes of credentials, by description.
SECRET_SHAPES: dict[str, re.Pattern[str]] = {
    "a GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    "a private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "an AWS access key id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "a JSON Web Token": re.compile(
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    ),
}
#: The runner's own paths. Their values are read from the environment as well.
HOST_PATH_VARIABLES = ("RUNNER_TEMP", "RUNNER_WORKSPACE", "GITHUB_WORKSPACE", "HOME")
HOST_PATHS = ("/home/runner",)


class CheckFailedError(Exception):
    """The SBOM does not hold to the check; the message says how."""


@dataclass(frozen=True)
class Component:
    type: str
    name: str
    version: str
    purl: str


def load(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CheckFailedError(f"{path}: no SBOM was written") from exc
    except json.JSONDecodeError as exc:
        raise CheckFailedError(f"{path}: not JSON ({exc})") from exc
    if not isinstance(document, dict):
        raise CheckFailedError(f"{path}: not a JSON object")
    return document


def components(document: Mapping[str, Any]) -> list[Component]:
    found = []
    for item in document.get("components") or []:
        if not isinstance(item, dict):
            raise CheckFailedError("a component is not an object")
        found.append(
            Component(
                type=str(item.get("type", "")),
                name=str(item.get("name", "")),
                version=str(item.get("version", "")),
                purl=str(item.get("purl", "")),
            )
        )
    return found


def schema_errors(text: str) -> str | None:
    """CycloneDX 1.6 strict schema validation; the error, or None. Imported here, not at the top:
    the library is the checker's own lock, not a dependency of the test environment, so mypy
    (run there) cannot see it."""
    from cyclonedx.schema import SchemaVersion  # type: ignore[import-not-found]
    from cyclonedx.validation.json import JsonStrictValidator  # type: ignore[import-not-found]

    error = JsonStrictValidator(SchemaVersion.V1_6).validate_str(text)
    if error is None:
        return None
    # The jsonschema error's own message; str() of it appends the whole schema.
    return str(getattr(error.data, "message", error))[:300]


def validate(path: Path, schema: Callable[[str], str | None] | None = None) -> int:
    """Raise unless the document is a schema-valid CycloneDX 1.6 SBOM from the pinned Syft."""
    document = load(path)
    if document.get("bomFormat") != "CycloneDX":
        raise CheckFailedError(f"{path}: bomFormat is {document.get('bomFormat')!r}")
    if document.get("specVersion") != SPEC_VERSION:
        raise CheckFailedError(f"{path}: specVersion is {document.get('specVersion')!r}")
    error = (schema or schema_errors)(path.read_text(encoding="utf-8"))
    if error is not None:
        raise CheckFailedError(
            f"{path}: invalid against the CycloneDX {SPEC_VERSION} schema: {error}"
        )
    tools = (document.get("metadata") or {}).get("tools") or {}
    generators = {
        (str(tool.get("name")), str(tool.get("version")))
        for tool in (tools.get("components") or [])
        if isinstance(tool, dict)
    }
    if ("syft", SYFT_VERSION) not in generators:
        raise CheckFailedError(f"{path}: not written by Syft {SYFT_VERSION} ({sorted(generators)})")
    listed = components(document)
    if not listed:
        raise CheckFailedError(f"{path}: lists no component")
    return len(listed)


def operating_system(listed: Sequence[Component]) -> Component:
    systems = [c for c in listed if c.type == "operating-system"]
    if len(systems) != 1:
        raise CheckFailedError(f"expected one operating-system component, found {len(systems)}")
    return systems[0]


def _same_version(a: str, b: str) -> bool:
    try:
        return Version(a) == Version(b)
    except InvalidVersion:
        return a == b


def lock_entries(text: str) -> list[tuple[str, str, str]]:
    """(name, version, marker) of every ``name==version`` entry of a uv-generated lock."""
    entries = []
    for line in text.splitlines():
        match = re.match(
            r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^ ;\\]+)\s*(?:;\s*([^\\]+?))?\s*\\?$", line
        )
        if match:
            entries.append((match.group(1), match.group(2), (match.group(3) or "").strip()))
    return entries


def image_installs(marker: str) -> bool:
    return not marker or Marker(marker).evaluate(environment=IMAGE_ENVIRONMENT)


def check_backend(path: Path, lock: Path) -> tuple[int, int]:
    """Every lock entry the image installs is a pkg:pypi component at its exact version."""
    listed = components(load(path))
    system = operating_system(listed)
    pypi: dict[str, list[str]] = {}
    for component in listed:
        if component.purl.startswith("pkg:pypi/"):
            pypi.setdefault(canonicalize_name(component.name), []).append(component.version)
    expected = [
        (name, version)
        for name, version, marker in lock_entries(lock.read_text(encoding="utf-8"))
        if image_installs(marker)
    ]
    if not expected:
        raise CheckFailedError(f"{lock}: no entry read; the lock parser is wrong")
    missing = [
        f"{name}=={version}"
        for name, version in expected
        if not any(_same_version(found, version) for found in pypi.get(canonicalize_name(name), []))
    ]
    if missing:
        raise CheckFailedError(
            f"{path}: {len(missing)} of {len(expected)} locked packages missing or at another "
            f"version: {', '.join(missing)}"
        )
    print(f"  OK   {len(expected)} of {len(expected)} locked packages at their exact versions")
    print(f"  OK   operating system: {system.name} {system.version}")
    return len(expected), len(listed)


def check_frontend(path: Path, nginx_version: str) -> int:
    """The served nginx is a component at the pinned version; the OS is recorded."""
    listed = components(load(path))
    system = operating_system(listed)
    nginx = [c for c in listed if c.name == "nginx" and c.type != "operating-system"]
    if not any(
        c.version == nginx_version or c.version.startswith(f"{nginx_version}-") for c in nginx
    ):
        raise CheckFailedError(
            f"{path}: no nginx {nginx_version} component ({[c.version for c in nginx]})"
        )
    print(
        f"  OK   nginx {nginx_version} catalogued ({', '.join(sorted({c.version for c in nginx}))})"
    )
    print(f"  OK   operating system: {system.name} {system.version}")
    return len(listed)


def identity(document: Mapping[str, Any]) -> Counter[tuple[str, str, str, str]]:
    return Counter((c.type, c.name, c.version, c.purl) for c in components(document))


def check_same(a: Path, b: Path) -> int:
    """Two builds' SBOMs list the same components."""
    first, second = identity(load(a)), identity(load(b))
    if first != second:
        only_a = sorted((first - second).elements())[:20]
        only_b = sorted((second - first).elements())[:20]
        raise CheckFailedError(
            f"the two builds' SBOMs differ: only in {a.name}: {only_a}; only in {b.name}: {only_b}"
        )
    total = sum(first.values())
    print(f"  OK   {total} components, the same in both builds")
    return total


def check_clean(path: Path, environ: Mapping[str, str] | None = None) -> None:
    """Raise if the document carries a host path, a credential's value or a credential's shape."""
    environ = os.environ if environ is None else environ
    text = path.read_text(encoding="utf-8")
    problems = [f"the runner path {host}" for host in HOST_PATHS if host in text]
    for name in HOST_PATH_VARIABLES:
        value = environ.get(name, "")
        if len(value) > 1 and value in text:
            problems.append(f"the value of ${name}")
    for name, value in sorted(environ.items()):
        if SECRET_NAME.search(name.upper()) and len(value) >= MIN_SECRET_LENGTH and value in text:
            problems.append(f"the value of ${name}")
    problems += [description for description, shape in SECRET_SHAPES.items() if shape.search(text)]
    if problems:
        raise CheckFailedError(f"{path}: carries {'; '.join(sorted(set(problems)))}")
    print(f"  OK   {path.name}: no host path, no credential value, no credential shape")


def main(argv: Sequence[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    command, args = argv[1], [Path(a) for a in argv[2:]]
    try:
        if command == "validate" and args:
            for path in args:
                print(f"  OK   {path.name}: CycloneDX {SPEC_VERSION}, schema-valid, Syft "
                      f"{SYFT_VERSION}, {validate(path)} components")  # fmt: skip
        elif command == "expect-invalid" and len(args) == 1:
            try:
                validate(args[0])
            except CheckFailedError as exc:
                print(f"  OK   the invalid control is rejected: {exc}")
            else:
                raise CheckFailedError(
                    f"{args[0]}: an invalid SBOM was ACCEPTED; the validator checks nothing"
                )
        elif command == "backend" and len(args) == 2:
            check_backend(args[0], args[1])
        elif command == "frontend" and len(args) == 2:
            check_frontend(args[0], str(args[1]))
        elif command == "same" and len(args) == 2:
            check_same(args[0], args[1])
        elif command == "clean" and args:
            for path in args:
                check_clean(path)
        else:
            print(__doc__)
            return 2
    except CheckFailedError as exc:
        print(f"::error::{exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
