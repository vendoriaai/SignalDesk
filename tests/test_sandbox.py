import pytest

from signaldesk.sandbox.executor import (
    SandboxTimeout,
    SandboxViolation,
    run_script,
)


def test_forbidden_import_rejected(tmp_path):
    with pytest.raises(SandboxViolation):
        run_script("import os\nprint(os.getcwd())", tmp_path)


def test_network_import_rejected(tmp_path):
    with pytest.raises(SandboxViolation):
        run_script("import requests\nprint(1)", tmp_path)


def test_timeout_enforced(tmp_path):
    with pytest.raises(SandboxTimeout):
        run_script("import time\ntime.sleep(30)", tmp_path, timeout=2,
                   allowed={"time"})


def test_happy_path_writes_output(tmp_path):
    proc = run_script(
        "import json\nfrom pathlib import Path\n"
        "Path('output').mkdir(exist_ok=True)\n"
        "Path('output/result.csv').write_text('a,b\\n1,2\\n')\n"
        "print(json.dumps({'ok': True}))",
        tmp_path,
    )
    assert "ok" in proc.stdout
    assert (tmp_path / "output" / "result.csv").exists()


def test_child_imports_the_same_signaldesk_package(tmp_path):
    import signaldesk

    proc = run_script("import signaldesk\nprint(signaldesk.__file__)", tmp_path)
    child_path = proc.stdout.strip()
    assert child_path == str(signaldesk.__file__), (
        "sandbox child resolved a different signaldesk package "
        f"({child_path} != {signaldesk.__file__})"
    )