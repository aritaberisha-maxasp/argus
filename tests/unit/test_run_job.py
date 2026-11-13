"""tools/azure/run_job.sh starts a Container Apps job and follows it to the end (#124, #148).

A stand in for the Azure CLI on the PATH answers like Azure. The script runs in Azure only from
the Demo workflow, so this is where its handling of Azure's answers is checked.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "azure" / "run_job.sh"

# START_BODY is what the start answers with; STATUS what the execution reports.
FAKE_AZ = """#!/usr/bin/env bash
args=" $* "
if [[ "$1 $2" == "resource show" ]]; then
  echo "/subscriptions/x/resourceGroups/rg-test/providers/Microsoft.App/jobs/caj-test"
elif [[ "$args" == *"/start?"* ]]; then
  [[ -n "$START_BODY" ]] && echo "$START_BODY"
elif [[ "$args" == *"/executions?"* ]]; then
  echo caj-test-newest
elif [[ "$args" == *"/executions/"* ]]; then
  echo "$args" >> "$CALLS"
  echo "$STATUS"
fi
exit 0
"""


def run(tmp_path: Path, start_body: str, status: str) -> subprocess.CompletedProcess[str]:
    fake = tmp_path / "az"
    fake.write_text(FAKE_AZ, encoding="utf-8")
    fake.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "RESOURCE_GROUP": "rg-test",
        "START_BODY": start_body,
        "STATUS": status,
        "CALLS": str(tmp_path / "calls"),
    }
    return subprocess.run(  # noqa: S603  the repository's own script
        ["bash", str(SCRIPT), "caj-test", "30"],  # noqa: S607  bash from the PATH
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")


def test_follows_the_execution_azure_names(tmp_path: Path) -> None:
    result = run(tmp_path, "caj-test-abc", "Succeeded")
    assert result.returncode == 0, result.stderr
    assert "execution caj-test-abc" in result.stdout
    assert "/executions/caj-test-abc?" in (tmp_path / "calls").read_text(encoding="utf-8")


def test_follows_the_newest_execution_when_the_start_has_no_body(tmp_path: Path) -> None:
    result = run(tmp_path, "", "Succeeded")
    assert result.returncode == 0, result.stderr
    assert "execution caj-test-newest" in result.stdout


def test_a_failed_job_fails_the_step(tmp_path: Path) -> None:
    result = run(tmp_path, "caj-test-abc", "Failed")
    assert result.returncode == 1
    assert "ended as Failed" in result.stderr
