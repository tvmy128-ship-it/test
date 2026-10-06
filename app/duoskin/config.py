"""Paths and settings IO (APP_SPEC §5.3, §14).

Public interface (APP_SPEC §5.4)::

    paths(home=None) -> DataPaths          # the DATA folder layout (spec name ``Paths``; aliased below)
    load_settings(home=None) -> Settings
    save_settings(s, home=None) -> None     # atomic: temp file + os.replace with retries
    effective_settings() -> Settings        # the running app's settings (with demo/provider overrides), else from disk

DATA resolution order: explicit ``home`` argument, ``DUOSKIN_HOME``, ``portable.flag`` next to the app (-> ``.\\data\\``),
``platformdirs.user_data_dir("DuoSkin", appauthor=False)`` (``%LOCALAPPDATA%\\DuoSkin``). Keys are never stored here.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from duoskin import winplat
from duoskin.models.settings import SETTINGS_SCHEMA_VERSION, Settings

log = logging.getLogger("duoskin.config")

APP_ROOT = Path(__file__).resolve().parent.parent
PORTABLE_FLAG = APP_ROOT / "portable.flag"
SETTINGS_FILE = "settings.json"


@dataclass(frozen=True)
class DataPaths:
    """Every folder and file the app owns under DATA (§5.3)."""

    home: Path

    @property
    def settings(self) -> Path:
        return self.home / SETTINGS_FILE

    @property
    def db(self) -> Path:
        return self.home / "duoskin.sqlite3"

    @property
    def secrets_dpapi(self) -> Path:
        return self.home / "secrets.dpapi"

    @property
    def secrets_dev(self) -> Path:
        return self.home / "secrets.dev.json"     # non-Windows development fallback only

    @property
    def cas_dir(self) -> Path:
        return self.home / "cas"

    @property
    def kits_dir(self) -> Path:
        return self.home / "kits"

    @property
    def models_dir(self) -> Path:
        return self.home / "models"

    @property
    def regression_dir(self) -> Path:
        return self.home / "regression"

    @property
    def user_data_dir(self) -> Path:
        return self.home / "user_data"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def run_dir(self) -> Path:
        return self.home / "run"

    @property
    def tmp_dir(self) -> Path:
        return self.home / "tmp"

    @property
    def backups_dir(self) -> Path:
        return self.home / "backups"

    @property
    def server_json(self) -> Path:
        return self.run_dir / "server.json"

    def ensure(self) -> DataPaths:
        for d in (self.home, self.cas_dir, self.kits_dir, self.models_dir, self.regression_dir, self.user_data_dir,
                  self.logs_dir, self.run_dir, self.tmp_dir, self.backups_dir):
            d.mkdir(parents=True, exist_ok=True)
        return self


Paths = DataPaths   # the spec calls this class ``Paths``; ``models.settings.Paths`` is the (different) settings section


def resolve_home(home: str | os.PathLike[str] | None = None) -> Path:
    if home is not None:
        return Path(home)
    env = os.environ.get("DUOSKIN_HOME")
    if env:
        return Path(env)
    if PORTABLE_FLAG.exists():
        return APP_ROOT / "data"
    from platformdirs import user_data_dir

    return Path(user_data_dir("DuoSkin", appauthor=False))


def paths(home: str | os.PathLike[str] | None = None, *, create: bool = True) -> DataPaths:
    p = DataPaths(resolve_home(home))
    return p.ensure() if create else p


# ---------------------------------------------------------------------------------------------------------- settings IO
def _migrate(data: dict[str, Any]) -> dict[str, Any]:
    """Upgrade an older ``settings.json`` to the current schema. v1 is the first version, so this only stamps it."""
    version = int(data.get("schema_version", 0) or 0)
    if version < 1:
        data["schema_version"] = 1
    if int(data["schema_version"]) > SETTINGS_SCHEMA_VERSION:
        raise ValueError(f"settings.json is from a newer app version (schema {data['schema_version']})")
    return data


def load_settings(home: str | os.PathLike[str] | None = None) -> Settings:
    """Read ``settings.json``; a missing file gives the defaults. A corrupt or unknown-field file is moved aside to
    ``settings.corrupt-<ts>.json`` and the defaults are used, so the app still starts (the problem is logged)."""
    path = DataPaths(resolve_home(home)).settings
    if not path.exists():
        return Settings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))   # Notepad and PowerShell 5 write a BOM
        if not isinstance(raw, dict):
            raise TypeError("settings.json is not a JSON object")
        return Settings.model_validate(_migrate(raw))
    except (ValueError, TypeError, OSError) as exc:   # JSONDecodeError and pydantic's ValidationError are ValueErrors
        aside = path.with_name(f"settings.corrupt-{int(time.time())}.json")
        try:
            winplat.replace_with_retry(path, aside)
        except OSError:
            log.exception("could not move the bad settings file aside")
        log.error("settings.json could not be used (%s); defaults loaded, old file kept as %s", exc, aside.name)
        return Settings()


def save_settings(settings: Settings, home: str | os.PathLike[str] | None = None) -> None:
    path = DataPaths(resolve_home(home)).settings
    payload = json.dumps(settings.model_dump(mode="json"), indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    winplat.atomic_write(path, payload.encode("utf-8"))


# ---------------------------------------------------------------------------------------------------------- user folders
_PCT_VAR = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")


def expand_path(text: str) -> Path:
    """Expand ``%VAR%`` (and ``$VAR``) in a user-configured path. ``%USERPROFILE%`` falls back to the home folder, and
    backslashes become ``/`` off Windows, so the default settings also work on a Linux development machine."""

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in os.environ:
            return os.environ[name]
        if name.upper() == "USERPROFILE":
            return str(Path.home())
        return match.group(0)

    out = os.path.expandvars(_PCT_VAR.sub(_sub, text))
    if not winplat.IS_WINDOWS:
        out = out.replace("\\", "/")
    return Path(out)


def exports_root(settings: Settings) -> Path:
    return expand_path(settings.paths.exports_root)


def tripo_inbox(settings: Settings) -> Path:
    return expand_path(settings.paths.tripo_inbox)


# ---------------------------------------------------------------------------------------------------------- server.json
def write_server_info(p: DataPaths, *, port: int, instance_id: str, pid: int | None = None) -> None:
    info = {"port": port, "instance_id": instance_id, "pid": pid if pid is not None else os.getpid(),
            "url": f"http://127.0.0.1:{port}/"}
    winplat.atomic_write(p.server_json, (json.dumps(info) + "\n").encode("utf-8"))


def read_server_info(p: DataPaths) -> dict[str, Any] | None:
    try:
        data = json.loads(p.server_json.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------------------------------------- active runtime
_active: dict[str, Any] = {"runtime": None}


def register_runtime(rt: Any) -> None:
    """Called by ``create_app``: the most recently created app is the "active" one that ``effective_settings`` reads."""
    _active["runtime"] = rt


def active_runtime() -> Any | None:
    return _active["runtime"]


def effective_settings() -> Settings:
    """Settings of the running app (demo mode and the ``providers_mode`` override applied); from disk when none runs.

    The provider registry uses this to decide ``real | mock | disabled`` per provider.
    """
    rt = _active["runtime"]
    if rt is not None:
        return rt.effective_settings()
    return load_settings()
