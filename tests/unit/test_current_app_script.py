"""tools/azure/current_app.sh with a fake az on the PATH (#81, review of PR 110).

The placeholder may only come back when Azure says the app does not exist. Any error from az
must stop the run, or a passing fault would put the placeholder over the running app.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tools" / "azure" / "current_app.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")


def run_with_fake_az(tmp_path: Path, az_body: str) -> subprocess.CompletedProcess[str]:
    fake = tmp_path / "bin" / "az"
    fake.parent.mkdir()
    fake.write_text("#!/usr/bin/env bash\n" + az_body, encoding="utf-8")
    fake.chmod(0o755)
    environ = {
        **os.environ,
        "PATH": f"{fake.parent}{os.pathsep}{os.environ['PATH']}",
        "RESOURCE_GROUP": "rg-argus",
        "APP_NAME": "ca-argus-web",
        "PLACEHOLDER_IMAGE": "placeholder:latest",
        "PLACEHOLDER_PORT": "80",
    }
    return subprocess.run(  # noqa: S603
        [str(BASH), str(SCRIPT)], env=environ, capture_output=True, text=True, check=False
    )


def test_no_app_yet_gives_the_placeholder(tmp_path: Path) -> None:
    result = run_with_fake_az(tmp_path, "echo 0\n")

    assert result.returncode == 0
    assert result.stdout == "image=placeholder:latest\nport=80\n"


def test_a_running_app_keeps_its_image(tmp_path: Path) -> None:
    body = (
        "if [ \"$2\" = list ]; then echo 1; else printf 'cr.azurecr.io/argus:abc\\n8000\\n'; fi\n"
    )
    result = run_with_fake_az(tmp_path, body)

    assert result.returncode == 0
    assert result.stdout == "image=cr.azurecr.io/argus:abc\nport=8000\n"


@pytest.mark.parametrize(
    "body",
    [
        'echo "ERROR: (TooManyRequests) throttled" >&2; exit 1\n',
        'if [ "$2" = list ]; then echo 1; else echo "ERROR: expired" >&2; exit 1; fi\n',
        "echo 2\n",
    ],
)
def test_any_error_stops_instead_of_using_the_placeholder(tmp_path: Path, body: str) -> None:
    result = run_with_fake_az(tmp_path, body)

    assert result.returncode != 0
    assert "placeholder" not in result.stdout
