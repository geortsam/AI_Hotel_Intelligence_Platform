"""R2.7T (L1): the language-model tests do not depend on the developer's shell.

R2.7S found two tests that failed when `OLLAMA_NUM_CTX` or `LLM_MODEL` was exported, because
`Settings` reads them from the process environment and their fixtures did not clear them. The
root `conftest.py` now removes every `LLM_*` and `OLLAMA_*` variable before each test.

This runs those tests' modules in a FRESH pytest process whose environment has the variables
preconfigured -- the situation of a developer who uses Ollama elsewhere -- and requires them to
pass exactly as they do in a clean shell. Nothing reaches a model: the modules under test refuse
every socket themselves, and no database URL is passed on.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

#: The ambient values that used to change two tests' outcomes, plus the model id.
AMBIENT = {
    "OLLAMA_NUM_CTX": "8192",
    "OLLAMA_TEMPERATURE": "0.9",
    "OLLAMA_SEED": "1",
    "LLM_MODEL": "some-other-model:latest",
}

#: The modules R2.7S named (and the adapter's own), each holding settings-sensitive tests.
MODULES = (
    "tests/evaluation/test_ollama_kpis_offline.py",
    "tests/evaluation/test_live_eval_script.py",
    "tests/backend/test_ollama_provider.py",
)


def test_the_ollama_tests_pass_with_llm_settings_exported_in_the_shell() -> None:
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in ("TEST_DATABASE_URL", "DATABASE_URL", "TESTDB_ADMIN_URL")
    }
    env.update(AMBIENT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    result = subprocess.run(
        [sys.executable, "-m", "pytest", *MODULES, "-p", "no:cacheprovider", "-q"],
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stdout[-3000:]
