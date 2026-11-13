"""tools/azure/apply_infra.sh prints only step outputs to stdout (#81, #138).

The deploy writes the script's stdout straight into GITHUB_OUTPUT. Any other line there, such
as the retry warning, makes GitHub reject the whole step even when Azure succeeded.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "azure" / "apply_infra.sh"
OUTPUT_LINE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*=.*$")

# A stand in for the Azure CLI: fails the first deployment, succeeds on the retry.
FAKE_AZ = """#!/usr/bin/env bash
count_file="$(dirname "$0")/calls"
calls=$(cat "$count_file" 2>/dev/null || echo 0)
echo $((calls + 1)) > "$count_file"
if [ "$calls" = 0 ]; then
  echo "first try fails" >&2
  exit 1
fi
echo '{"registryName": {"value": "crargus"}, "appUrl": {"value": "https://app.example"}}'
"""


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed")
def test_retry_keeps_stdout_to_step_outputs(tmp_path: Path) -> None:
    fake = tmp_path / "az"
    fake.write_text(FAKE_AZ, encoding="utf-8")
    fake.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "RETRY_AFTER_SECONDS": "0",
        "RESOURCE_GROUP": "rg-test",
    }

    result = subprocess.run(  # noqa: S603  the repository's own script
        ["bash", str(SCRIPT), "argus-infra-test"],  # noqa: S607  bash from the PATH
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines == ["registryName=crargus", "appUrl=https://app.example"]
    assert all(OUTPUT_LINE.match(line) for line in lines)
    assert "runs once more" in result.stderr
