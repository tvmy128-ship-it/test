"""``python -m duoskin <command>`` (APP_SPEC §15.4). Always launch with ``-m duoskin`` from the app root.

Commands: ``run [--open-browser] [--port N]``, ``selfcheck``, ``doctor [--setup] [--json]``, ``reset-leases``,
``export-diagnostics``, ``gc [--dry-run]``, ``regression [--stage plan|parts] [--template ID] [--sample N]
[--candidate role=version] [--compare] [--real] [--yes]`` (the staged regression and the variety guard, APP_SPEC §3.9) and
``calibrate-report [--week W] [--json]`` (the weekly report, §3.9); ``build-kit-manifest`` and ``build-head-base <source> --variant <name>``
belong to later milestones and say so (their arguments are parsed so the later tracks only fill in the body).
"""
import os

# SYS-17: pin native thread pools before numpy (or anything that imports it) loads.
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ.setdefault("PYTHONUTF8", "1")

import argparse
import faulthandler
import importlib
import json
import logging
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

log = logging.getLogger("duoskin.main")

SELFCHECK_MODULES = ("duoskin", "fastapi", "starlette", "uvicorn", "pydantic", "sse_starlette", "multipart", "httpx", "keyring",
                     "platformdirs", "psutil", "yaml", "truststore", "PIL", "numpy")


#: SDK / HTTP-library switches that turn on request logging (headers, URLs). They are dropped at start: a key must never reach a log.
DEBUG_LOG_ENV_VARS = ("ANTHROPIC_LOG", "OPENAI_LOG", "HTTPX_LOG_LEVEL", "HTTPX2_LOG_LEVEL", "HTTP_DEBUG")


def _early_setup() -> None:
    """Very first steps of ``run`` (APP_SPEC §15.4 step 1): faulthandler, certificate store, DLL directories, MIME fix."""
    for name in DEBUG_LOG_ENV_VARS:
        os.environ.pop(name, None)
    try:
        faulthandler.enable()
    except (RuntimeError, ValueError, OSError):
        pass
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:  # noqa: BLE001, S110
        pass
    from duoskin import winplat

    winplat.fix_dll_directories()
    winplat.fix_mimetypes()
    try:                                       # SYS-17: the step threads already use every core; OpenCV's own pool would oversubscribe
        import cv2

        cv2.setNumThreads(1)
    except Exception:  # noqa: BLE001, S110 - a missing or broken OpenCV is reported by the doctor, not here
        pass


# ------------------------------------------------------------------------------------------------------------ commands
def cmd_selfcheck(_args: argparse.Namespace) -> int:
    missing = []
    for name in SELFCHECK_MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            missing.append(f"{name} ({type(exc).__name__})")
    if missing:
        print("Missing or broken: " + ", ".join(missing))
        return 1
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    _early_setup()
    from duoskin.api.doctor_checks import format_report, run_doctor
    from duoskin.engine.runtime import Runtime

    rt = Runtime.create(args.home, providers_mode=args.providers, make_default=True)
    try:
        rt.started_via_module = True
        report = run_doctor(rt, setup=args.setup, quick=args.quick)
        rt.set_doctor_report(report)
    finally:
        rt.shutdown()
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(format_report(report))
    return int(report["exit_code"])


def cmd_reset_leases(args: argparse.Namespace) -> int:
    from duoskin import config, winplat
    from duoskin.engine.recovery import recover
    from duoskin.engine.runtime import Runtime

    paths = config.paths(args.home)
    lock = winplat.single_instance(paths.run_dir / "instance.lock")
    if lock is None:
        print("DuoSkin Studio is running. Close it first: resetting leases under a running app would break its steps.")
        return 1
    rt = Runtime.create(args.home, providers_mode="mock")
    try:
        report = recover(rt)
    finally:
        rt.shutdown()
        lock.release()
    print(f"{len(report.to_waiting_remote)} step(s) resume polling, {len(report.requeued)} re-queued, "
          f"{len(report.failed)} failed (interrupted too often), {report.orphaned_costs} cost row(s) marked orphan, "
          f"{len(report.killed_children)} child process(es) killed.")
    return 0


def cmd_export_diagnostics(args: argparse.Namespace) -> int:
    from duoskin.engine.diagnostics import export_diagnostics
    from duoskin.engine.runtime import Runtime

    rt = Runtime.create(args.home, providers_mode="mock")
    try:
        print(export_diagnostics(rt))
    finally:
        rt.shutdown()
    return 0


def cmd_gc(args: argparse.Namespace) -> int:
    from duoskin import config, winplat
    from duoskin.engine.gc import gc as run_gc
    from duoskin.engine.runtime import Runtime

    paths = config.paths(args.home)
    lock = None
    if not args.dry_run:
        lock = winplat.single_instance(paths.run_dir / "instance.lock")
        if lock is None:
            print("DuoSkin Studio is running. Close it before deleting files (or use --dry-run).")
            return 1
    rt = Runtime.create(args.home, providers_mode="mock")
    try:
        report = run_gc(rt, dry_run=True, older_than_days=args.days)
        print(report.summary())
        if args.dry_run or not report.candidates and not report.orphan_files:
            return 0
        if not args.yes:
            answer = input("Delete these files now? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("Nothing deleted.")
                return 0
        done = run_gc(rt, dry_run=False, older_than_days=args.days)
        print(done.summary())
        return 0
    finally:
        rt.shutdown()
        if lock is not None:
            lock.release()


def _parse_candidates(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        role, sep, version = item.partition("=")
        if not sep or not role.strip() or not version.strip():
            raise ValueError(f"--candidate wants ROLE=VERSION, got '{item}'")
        out[role.strip()] = version.strip()
    return out


def _print_run(run: dict, guard: dict | None) -> None:
    m = run.get("metrics") or {}
    print(f"  {run['role']:<9} {run['state']:<8} briefs {run.get('ok_briefs', 0)}/{run.get('n_briefs', 0)}   quality {run.get('quality', 0):.3f}   "
          f"picture distance {m.get('image_distance', 0):.3f} ({run.get('image_mode', '?')})   variety index {m.get('variety_index', 0):.3f}   "
          f"cost ${run.get('cost_usd', 0):.2f}")
    if guard:
        print(f"  guard: {guard['decision'].upper()}")
        for reason in guard["reasons"]:
            print(f"    - {reason}")


def cmd_regression(args: argparse.Namespace) -> int:
    """The staged regression and the variety guard (APP_SPEC §3.9). Mock providers unless ``--real``; shows the estimate and asks first."""
    from duoskin import config, winplat
    from duoskin.app import load_plugins
    from duoskin.engine.runtime import Runtime
    from duoskin.models.job import TERMINAL_JOB_STATES
    from duoskin.pipeline import regression as reg

    try:
        candidates = _parse_candidates(args.candidate)
    except ValueError as exc:
        print(str(exc))
        return 1
    paths = config.paths(args.home)
    lock = winplat.single_instance(paths.run_dir / "instance.lock")
    if lock is None:
        print("DuoSkin Studio is running. Use the Learning page, or close the app and run this again.")
        return 1
    providers = args.providers or (None if args.real else "mock")
    rt = Runtime.create(args.home, providers_mode=providers)
    try:
        load_plugins(rt)
        rt.startup(backup=False)
        try:
            pre = reg.preview_regression(rt, stage=args.stage, sample=args.sample, templates=args.template, candidate_versions=candidates, compare=args.compare)
        except reg.RegressionError as exc:
            print(str(exc))
            return 1
        est = pre["estimate"]
        print(f"Regression test, stage {pre['stage']}: {pre['items']} {'briefs' if pre['stage'] == 'plan' else 'frozen specs'}, "
              f"runs: {', '.join(pre['arms'])}, providers: {pre['mode']}.")
        if pre["brief_set_edited"]:
            print("The brief set was edited by you; its checksum is part of the result.")
        print(est["text"])
        if est["needs_confirmation"]:
            print(f"This is above ${est['ask_above_usd']:.0f}: your answer below is the budget confirmation.")
        if est["needs_confirmation"] and args.yes:
            print(f"--yes cannot confirm a paid run above ${est['ask_above_usd']:.0f}: run it again without --yes and answer the question.")
            return 1
        if not args.yes and input("Start it now? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing started.")
            return 0
        job = reg.start_regression(rt, stage=args.stage, sample=args.sample, templates=args.template, candidate_versions=candidates, compare=args.compare)
        confirmed = False
        last = ""
        try:
            while rt.repo.get_job(job.id).state not in TERMINAL_JOB_STATES:
                time.sleep(1.0)
                from duoskin.models.gate import GateAction, GateDecisionIn, GateKind

                for g in rt.repo.list_gates(state="open"):
                    if g.kind == GateKind.BUDGET and g.job_id == job.id and not confirmed:
                        tile = g.tiles[0]
                        rt.gates.decide(g.id, GateDecisionIn(tile_id=tile.tile_id, action=GateAction.CONTINUE, expected_version=tile.version,
                                                             client_decision_id=f"cli-{job.id}"))
                        confirmed = True
                        print("Budget confirmed.")
                counts = rt.repo.step_counts(job_id=job.id)
                line = f"  steps: {counts.get('succeeded', 0)} done, {counts.get('running', 0) + counts.get('waiting_remote', 0)} running, {counts.get('failed', 0)} failed"
                if line != last:
                    print(line)
                    last = line
        except KeyboardInterrupt:
            rt.scheduler.cancel_job(job.id)
            reg.sweep_override(rt)
            print("Cancelled.")
            return 130
        final = rt.repo.get_job(job.id)
        runs = [r for r in (reg.load_run(rt, rid) for rid in job.params["run_ids"]) if r]
        if final.state.value != "succeeded" or not runs or any(r.get("state") != "done" for r in runs):
            print(f"The regression test did not finish ({final.state.value}).")
            return 1
        print("Result:")
        code = 0
        for r in runs:
            _print_run(r, r.get("guard"))
            if r.get("guard") and not r["guard"]["accepted"]:
                code = 2
        return code
    finally:
        rt.shutdown()
        lock.release()


def cmd_calibrate_report(args: argparse.Namespace) -> int:
    """Print the weekly learning report (APP_SPEC §3.9): per-check flag and catch rates, alerts, labels, costs."""
    from duoskin.engine import calibration
    from duoskin.engine.runtime import Runtime

    rt = Runtime.create(args.home, providers_mode="mock")
    try:
        try:
            report = calibration.weekly_report(rt, args.week)
        except ValueError:
            print(f"'{args.week}' is not a week: use 2026-W41, a date, or 'all'.")
            return 1
    finally:
        rt.shutdown()
    print(json.dumps(report, indent=2, default=str) if args.json else calibration.format_report(report))
    return 0


def _later(name: str) -> int:
    print(f"'{name}' is not available in this build yet (it belongs to a later milestone).")
    return 1


def _wait_for_health(port: int, timeout_s: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout_s
    url = f"http://127.0.0.1:{port}/api/health"
    # urllib reads the Windows proxy settings from the registry: without this, a company proxy that lacks "<local>" would
    # answer for 127.0.0.1 and the browser would never open.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        try:
            with opener.open(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except OSError:
            time.sleep(0.3)
    return False


def cmd_run(args: argparse.Namespace) -> int:
    _early_setup()
    import uvicorn

    from duoskin import config, logsetup, winplat
    from duoskin.app import create_app
    from duoskin.engine.runtime import Runtime

    paths = config.paths(args.home)
    settings = config.load_settings(paths.home)
    logsetup.setup_logging(paths.logs_dir, console=True, dev=settings.dev_mode)
    lock = winplat.single_instance(paths.run_dir / "instance.lock")
    if lock is None:
        info = config.read_server_info(paths)
        if info and info.get("url"):
            print(f"DuoSkin Studio is already running at {info['url']}")
            if args.open_browser:
                webbrowser.open(str(info["url"]))
            return 0
        print("Another DuoSkin Studio instance holds the lock, but it has not published its address yet. Try again in a moment.")
        return 1

    rt = Runtime(paths, settings, providers_mode=args.providers)
    rt.started_via_module = True
    sock = winplat.bind_socket(args.port or settings.port)
    port = sock.getsockname()[1]
    rt.remember_port(port)
    config.write_server_info(paths, port=port, instance_id=rt.instance_id)
    app = create_app(runtime=rt, run_doctor_on_start=True)
    # proxy_headers off: nothing may rewrite the client address or scheme from an X-Forwarded-* header; server_header off: no "uvicorn" fingerprint
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, timeout_graceful_shutdown=2, lifespan="on",
                                           proxy_headers=False, server_header=False))

    def begin_shutdown() -> None:
        """Fast shutdown (APP_SPEC §4.3): stop claiming, cancel flags, end SSE streams, then exit within ~2 s."""
        if rt.shutting_down:
            return
        rt.scheduler.stop(timeout=0.5)
        rt.bus.close()
        server.should_exit = True

        def hard_exit() -> None:
            try:
                rt.db.checkpoint("TRUNCATE")
            except Exception:  # noqa: BLE001, S110
                pass
            os._exit(0)

        timer = threading.Timer(2.0, hard_exit)
        timer.daemon = True
        timer.start()

    rt.on_shutdown = begin_shutdown
    winplat.quickedit_off()
    handler = winplat.console_ctrl_handler(begin_shutdown)
    url = f"http://127.0.0.1:{port}/"
    print(f"DuoSkin Studio is running at {url}  (close this window to stop)")
    if args.open_browser:
        threading.Thread(target=lambda: _wait_for_health(port) and webbrowser.open(url), name="duoskin-open-browser",
                         daemon=True).start()
    try:
        server.run(sockets=[sock])
    finally:
        handler.remove()
        try:
            rt.shutdown()
        finally:
            try:
                paths.server_json.unlink()
            except OSError:
                pass
            lock.release()
    return 0


# ------------------------------------------------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m duoskin", description="DuoSkin Studio")
    p.add_argument("--home", type=Path, default=None, help="data folder (default: %%LOCALAPPDATA%%\\DuoSkin or DUOSKIN_HOME)")
    p.add_argument("--providers", default=None, help="provider modes, e.g. mock or anthropic:real,tripo:mock")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="start the app")
    run.add_argument("--open-browser", action="store_true")
    run.add_argument("--port", type=int, default=None)
    run.set_defaults(fn=cmd_run)

    sub.add_parser("selfcheck", help="import the package and its pure-Python dependencies").set_defaults(fn=cmd_selfcheck)

    doc = sub.add_parser("doctor", help="check this PC (CHK-S01..S15)")
    doc.add_argument("--setup", action="store_true", help="the run that follows setup.bat (no network probes)")
    doc.add_argument("--quick", action="store_true", help="skip the slow native-library probes")
    doc.add_argument("--json", action="store_true")
    doc.set_defaults(fn=cmd_doctor)

    sub.add_parser("reset-leases", help="recover steps left RUNNING by a crash").set_defaults(fn=cmd_reset_leases)
    sub.add_parser("export-diagnostics", help="write a redacted diagnostics zip").set_defaults(fn=cmd_export_diagnostics)

    gc = sub.add_parser("gc", help="delete unreferenced assets")
    gc.add_argument("--dry-run", action="store_true")
    gc.add_argument("--days", type=int, default=30)
    gc.add_argument("--yes", action="store_true", help="do not ask before deleting")
    gc.set_defaults(fn=cmd_gc)

    reg = sub.add_parser("regression", help="the staged regression + variety guard (mock providers unless --real)")
    reg.add_argument("--stage", choices=("plan", "parts"), default="plan")
    reg.add_argument("--template", action="append", default=[], metavar="ID", help="part template (I2, R1, ...) or part kind; stage parts only")
    reg.add_argument("--sample", type=int, default=None, help="run a deterministic sample of this many briefs or specs")
    reg.add_argument("--candidate", action="append", default=[], metavar="ROLE=VERSION", help="a candidate model snapshot to test, e.g. planner=claude-opus-5-20261001")
    reg.add_argument("--compare", action="store_true", help="judge a change that is already installed against the stored baseline")
    reg.add_argument("--real", action="store_true", help="use the saved providers (real keys) instead of mock; the estimate is shown first")
    reg.add_argument("--yes", action="store_true", help="do not ask before starting (never for a paid run above the regression ask threshold)")
    reg.set_defaults(fn=cmd_regression)
    head = sub.add_parser("build-head-base", help="build a head-base folder from a source mesh through Blender (later milestone)")
    head.add_argument("source", type=Path)
    head.add_argument("--variant", required=True)
    head.set_defaults(fn=lambda _a: _later("build-head-base"))
    sub.add_parser("build-kit-manifest", help="later milestone").set_defaults(fn=lambda _a: _later("build-kit-manifest"))
    rep = sub.add_parser("calibrate-report", help="print the weekly learning report")
    rep.add_argument("--week", default=None, help="2026-W41, a date in that week, or 'all' (default: this week)")
    rep.add_argument("--json", action="store_true")
    rep.set_defaults(fn=cmd_calibrate_report)
    return p


def main(argv: list[str] | None = None) -> int:
    from duoskin import logsetup, winplat

    winplat.configure_stdio()              # print() and the console log handler must not crash on a cp1252 console (SYS-06)

    logsetup.install_record_factory()      # every log record is born redacted, even before setup_logging (and for the CLI commands)
    args = build_parser().parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
