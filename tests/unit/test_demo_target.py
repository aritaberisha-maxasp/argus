"""tools/azure/demo_target.sh finds the demo targets in the ARGUS resource group (#124, #146).

The script runs against Azure only in the Demo workflow, so this test puts a stand in for the
Azure CLI on the PATH. Like the real one, the stand in refuses --tag together with
--resource-group, which broke the first Demo run in Azure.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "azure" / "demo_target.sh"

FAKE_AZ = """#!/usr/bin/env bash
args=" $* "
if [[ "$args" == *" --tag "* && "$args" == *" --resource-group "* ]]; then
  echo "ERROR: you cannot use '--tag' with '--resource-group'" >&2
  exit 1
fi
case "$1 $2" in
  "resource show")
    printf 'ARGUS_DEMO\\ttrue\\n'
    printf 'ARGUS_DATABASE_URL\\tpostgresql+psycopg://id-argus-app@psql-argus.example:5432/argus_demo?sslmode=require\\n'
    ;;
  "resource list")
    if [[ "$args" == *"role=='demo-standin'"* ]]; then
      printf 'ca-argus-standin-graph\\nca-argus-standin-wise\\n'
    elif [[ "$args" == *"role=='demo'"* ]]; then
      echo stargusdemo
    fi
    ;;
esac
"""


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_demo_targets_come_from_the_resource_group(tmp_path: Path) -> None:
    fake = tmp_path / "az"
    fake.write_text(FAKE_AZ, encoding="utf-8")
    fake.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "RESOURCE_GROUP": "rg-test",
        "APP_NAME": "ca-argus-web",
    }

    result = subprocess.run(  # noqa: S603  the repository's own script
        ["bash", str(SCRIPT)],  # noqa: S607  bash from the PATH
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "demo=true",
        "database_server=psql-argus",
        "database_host=psql-argus.example",
        "database_name=argus_demo",
        "documents_account=stargusdemo",
        "standins=ca-argus-standin-graph ca-argus-standin-wise",
    ]
