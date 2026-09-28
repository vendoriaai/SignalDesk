"""Sandboxed compute executor (TAD 3.3).

v0 guarantees:
- static import gate: only whitelisted modules may be imported
- separate subprocess with scrubbed environment (user PYTHONPATH/PYTHONSTARTUP
  removed; PYTHONPATH is then set to the resolved first-party package root so
  the whitelisted `signaldesk` import always resolves to the same code the
  parent process is running, even if the editable install points at a moved
  checkout)
- hard wall-clock timeout
- file exchange only through a dedicated workdir (input/ + output/)

Hardening on the roadmap: no user-site imports (`-I` currently breaks user-site
installs of pandas), OS-level network deny, memory caps (resource limits on
POSIX / Job Objects on Windows), and no first-party imports once the planner
starts emitting untrusted code. First-party `signaldesk` code is currently
trusted and importable.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

ALLOWED_IMPORTS = {
    "pandas", "numpy", "math", "statistics", "csv", "json", "datetime",
    "collections", "itertools", "functools", "pathlib",
    "matplotlib", "matplotlib.pyplot",  # chart rendering (Agg only)
    "signaldesk",  # first-party, trusted until the LLM planner lands
}

DEFAULT_TIMEOUT = 60  # seconds, per TAD


class SandboxViolation(Exception):
    """Raised before execution when the static gate rejects the script."""


class SandboxTimeout(Exception):
    """Execution exceeded the wall-clock budget."""


class SandboxError(Exception):
    """The script ran but exited non-zero."""


def _check_imports(source: str, allowed: set[str]) -> None:
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names if a.name.split(".")[0] not in allowed]
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root not in allowed:
                offenders.append(node.module or "")
    if offenders:
        joined = ", ".join(sorted(set(offenders)))
        raise SandboxViolation(f"imports outside whitelist {sorted(allowed)}: {joined}")


def run_script(
    source: str,
    workdir: Path,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    allowed: set[str] | None = None,
) -> subprocess.CompletedProcess:
    allowed = set(allowed or ALLOWED_IMPORTS)
    _check_imports(source, allowed)

    workdir = Path(workdir)
    (workdir / "output").mkdir(parents=True, exist_ok=True)
    script = workdir / "sandbox_script.py"
    script.write_text(source, encoding="utf-8")

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONSTARTUP", None)
    env["PYTHONIOENCODING"] = "utf-8"

    if getattr(sys, "frozen", False):
        # PyInstaller bundle: sys.executable is the app, not a Python
        # interpreter — spawn it in the hidden child mode that execs scripts.
        cmd = [sys.executable, "sandbox-exec", str(script)]
    else:
        import signaldesk

        # The child runs with cwd=workdir, far from the source tree; anchor
        # the whitelisted first-party import to the package this process
        # actually loaded, not to whatever site-packages happens to contain.
        env["PYTHONPATH"] = str(Path(signaldesk.__file__).resolve().parent.parent)
        cmd = [sys.executable, str(script)]

    try:
        return subprocess.run(
            cmd,
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise SandboxTimeout(f"sandbox exceeded {timeout}s") from exc
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or exc.stdout or "").strip().splitlines()[-5:]
        raise SandboxError("sandbox script failed:\n" + "\n".join(tail)) from exc
