import subprocess
import sys


def test_transport_import_does_not_resolve_type_checking_names() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import gui_gateway.transport"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
