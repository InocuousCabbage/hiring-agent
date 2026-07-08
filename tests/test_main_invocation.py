"""
tests/test_main_invocation.py — Regression guards for the README-documented
`python src/main.py` script-mode invocation.

Background: prior to the sys.path bootstrap in src/main.py, running
`python src/main.py --test` failed at run_pipeline (~line 369) with
`ModuleNotFoundError: No module named 'src.apply'`. Cause: Python's
script-mode invocation puts `os.path.dirname(script)` on sys.path (i.e. `src/`),
not the repo root, so `from src.apply import _seam` couldn't locate the
package. Module-mode invocation (`python -m src.main`) worked because Python's
package loader puts repo-root on sys.path.

The fix inserts repo-root into sys.path at the very top of src/main.py — before
any `from src.apply.*` imports — so both invocation modes work.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_main_script_mode_syspath_bootstrap(tmp_path):
    """RED: script-mode invocation must bootstrap repo-root onto sys.path so
    `from src.apply import _seam` resolves.

    Simulates `python src/main.py` in a subprocess by seeding sys.path with
    only `src/` (mirroring what CPython does for script-mode) and importing
    main. Post-fix, main's own bootstrap adds repo-root, and `src.apply`
    becomes importable.
    """
    snippet = textwrap.dedent(
        f"""
        import os, sys
        # Simulate script-mode: strip repo-root from sys.path, seed only src/
        # (this mirrors `python src/main.py` — CPython inserts the script's
        # directory as sys.path[0], NOT the repo root).
        _repo_root = {REPO_ROOT!r}
        sys.path[:] = [p for p in sys.path if os.path.abspath(p) != _repo_root]
        sys.path.insert(0, {os.path.join(REPO_ROOT, "src")!r})
        # Import main — its top-of-file bootstrap must re-add repo-root.
        import main  # noqa: F401
        # If bootstrap worked, this now succeeds:
        from src.apply import _seam  # noqa: F401
        print("BOOTSTRAP_OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=30,
    )
    assert "BOOTSTRAP_OK" in result.stdout, (
        "src/main.py did not bootstrap repo-root onto sys.path — "
        f"`from src.apply import _seam` failed under script-mode.\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert result.returncode == 0, (
        f"subprocess exited non-zero: rc={result.returncode}\n"
        f"stderr={result.stderr!r}"
    )


def test_main_module_mode_still_works():
    """GREEN sanity: `python -m src.main --help` must still succeed. The
    bootstrap must not break the previously-working module-mode invocation."""
    result = subprocess.run(
        [sys.executable, "-m", "src.main", "--help"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"`python -m src.main --help` broke:\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "usage:" in result.stdout.lower(), (
        f"expected usage output; got stdout={result.stdout!r}"
    )
