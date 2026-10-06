"""``python -m duoskin regression`` and ``calibrate-report`` (APP_SPEC §15.4)."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from duoskin import config

ENV = {**os.environ, "PYTHONPATH": str(config.APP_ROOT), "PYTHONUTF8": "1", "DUOSKIN_PROVIDERS": ""}


def cli(*args, stdin: str | None = None, timeout=600):
    return subprocess.run([sys.executable, "-m", "duoskin", *args], check=False, capture_output=True, encoding="utf-8", timeout=timeout,
                          cwd=str(config.APP_ROOT), env=ENV, input=stdin)


def test_the_weekly_report_prints_as_text_and_as_json(tmp_path):
    home = str(tmp_path / "h")
    text = cli("--home", home, "calibrate-report")
    assert text.returncode == 0 and "weekly report" in text.stdout and "Approved duos: 0" in text.stdout and "Labels: 0" in text.stdout
    j = cli("--home", home, "calibrate-report", "--json", "--week", "all")
    d = json.loads(j.stdout)
    assert j.returncode == 0 and d["week"]["label"] == "all time" and d["approved_duos"] == 0 and "per_check" in d and "regression" not in d
    bad = cli("--home", home, "calibrate-report", "--week", "never")
    assert bad.returncode == 1 and "not a week" in bad.stdout


def test_the_regression_command_validates_its_arguments_and_asks_before_starting(tmp_path):
    home = str(tmp_path / "h")
    assert cli("--home", home, "regression", "--candidate", "planner").returncode == 1
    r = cli("--home", home, "regression", "--candidate", "nonsense=x", "--yes")
    assert r.returncode == 1 and "not a model role" in r.stdout
    r = cli("--home", home, "regression", "--stage", "parts", "--template", "I2", "--yes")
    assert r.returncode == 1 and "approved duos to freeze" in r.stdout
    r = cli("--home", home, "regression", "--sample", "2", stdin="n\n")
    assert r.returncode == 0 and "2 briefs" in r.stdout and "Mock providers: nothing is charged." in r.stdout and "Nothing started." in r.stdout
    assert "Result:" not in r.stdout


@pytest.mark.timeout(900)
def test_a_mock_regression_runs_to_the_end_and_prints_its_numbers(tmp_path):
    home = str(tmp_path / "h")
    r = cli("--home", home, "regression", "--sample", "2", "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "providers: mock" in r.stdout and "Result:" in r.stdout and "baseline" in r.stdout and "briefs 2/2" in r.stdout and "variety index" in r.stdout
    again = cli("--home", home, "regression", "--sample", "2", "--compare", "--yes")
    assert again.returncode == 0 and "guard: ACCEPT" in again.stdout and "candidate" in again.stdout, again.stdout + again.stderr
