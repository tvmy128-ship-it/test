"""Settings model (APP_SPEC §14.1). Stored in ``DATA\\settings.json``; API keys are never stored here (see keystore).

``Settings`` is strict (``extra="forbid"``): a file with unknown fields is refused by ``config.load_settings`` and moved
aside, never half-applied. Model pins must be dated snapshots: ``-latest`` aliases are rejected by a validator.
"""
from __future__ import annotations

import copy
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator

from duoskin.models.common import Strict

SETTINGS_SCHEMA_VERSION = 1
KNOWN_PROVIDERS = ("anthropic", "openai", "recraft", "tripo", "gemini", "fal")


class ProviderMode(StrEnum):
    REAL = "real"
    MOCK = "mock"
    DISABLED = "disabled"


def _reject_alias(v: str) -> str:
    low = v.strip().lower()
    if not low:
        raise ValueError("model id must not be empty")
    if low.endswith("-latest") or low == "latest" or "-latest-" in low:
        raise ValueError("model aliases (-latest) are not allowed: pin a dated snapshot")
    return v.strip()


class ModelPins(Strict):
    """Defaults for NEW projects. A project pins its own copy (``VersionPins``)."""

    planner: str = "claude-opus-5"
    critic: str = "claude-opus-5"
    judge: str = "claude-opus-5"
    checker: str = "claude-sonnet-5"
    image_draft: str = "gpt-image-2.5-flare-2026-09-08"
    image_final: str = "gpt-image-2.5-sunburst-2026-09-08"
    recraft_face: str = "recraftv4_styles_vector"
    recraft_bootstrap: str = "recraftv4_1_utility_vector"
    recraft_print: str = "recraftv4_1_vector"
    tripo_mesh: str = "P2-20260801"
    tripo_fallback_p1: str = "P1-20260311"
    tripo_fallback_h31: str = "v3.1-20260211"
    gemini_judge: str = "gemini-3.8-flash"
    gemini_image: str = "gemini-3.1-flash-image"
    candidates: dict[str, str] = Field(default_factory=dict)   # role -> snapshot awaiting regression + variety guard

    @field_validator(
        "planner", "critic", "judge", "checker", "image_draft", "image_final", "recraft_face", "recraft_bootstrap",
        "recraft_print", "tripo_mesh", "tripo_fallback_p1", "tripo_fallback_h31", "gemini_judge", "gemini_image",
    )
    @classmethod
    def _no_alias(cls, v: str) -> str:
        return _reject_alias(v)

    @field_validator("candidates")
    @classmethod
    def _no_alias_candidates(cls, v: dict[str, str]) -> dict[str, str]:
        for value in v.values():
            _reject_alias(value)
        return v


class Budgets(Strict):
    per_duo_usd: float = Field(default=15.0, gt=0)      # thresholds: budget.per_duo_usd
    ask_above_usd: float = Field(default=2.0, ge=0)
    daily_cap_usd: float | None = Field(default=None, gt=0)
    tripo_credit_floor: int = Field(default=200, ge=0)  # banner when balance - frozen falls below


def _default_modes() -> dict[str, ProviderMode]:
    modes = {p: ProviderMode.REAL for p in ("anthropic", "openai", "recraft", "tripo", "gemini")}
    modes["fal"] = ProviderMode.DISABLED
    return modes


class ProviderSettings(Strict):
    modes: dict[str, ProviderMode] = Field(default_factory=_default_modes)
    openai_ipm: int = Field(default=5, ge=1)            # images per minute; drafts use n <= min(ipm, 4)
    anthropic_concurrency: int = Field(default=3, ge=1, le=16)
    openai_concurrency: int = Field(default=3, ge=1, le=16)
    recraft_concurrency: int = Field(default=2, ge=1, le=16)
    tripo_slots: int = Field(default=2, ge=1, le=16)
    gemini_key_billed: bool = False                     # CHK-P11: private images go to Gemini only when true

    @field_validator("modes")
    @classmethod
    def _known_providers(cls, v: dict[str, ProviderMode]) -> dict[str, ProviderMode]:
        unknown = sorted(set(v) - set(KNOWN_PROVIDERS))
        if unknown:
            raise ValueError(f"unknown provider(s): {', '.join(unknown)}")
        return v

    def concurrency_for(self, provider: str) -> int:
        return {
            "anthropic": self.anthropic_concurrency, "openai": self.openai_concurrency,
            "recraft": self.recraft_concurrency, "tripo": self.tripo_slots, "gemini": 2, "fal": 1,
        }.get(provider, 2)


class ThreeD(Strict):
    default_mesh_mode: Literal["api", "manual", "ask"] = "ask"
    blender_path: str | None = None                     # auto-detected on first run; validated with --version
    blender_tested_versions: list[str] = Field(default_factory=lambda: ["4.2", "4.5", "5.0", "5.1", "5.2"])
    studio_forward_axis: Literal["unknown", "+Z", "-Z"] = "unknown"
    hair_default_route: Literal["auto", "kit", "tripo_api", "manual"] = "auto"
    build_start_per_tile: bool = False


class ChecksSettings(Strict):
    reference_similarity_default: bool = False          # requirement 7: default OFF; per project in ProjectSettings
    sparkle_star_allowed: bool = True                   # bible D26 [UNVERIFIED policy]
    ratio_check: bool = True
    check_overrides: dict[str, Literal["soft"]] = Field(default_factory=dict)   # demoted checks; never roblox/ip/...
    cheap_critic_mode: bool = False
    gemini_second_opinion: bool = False
    face_route_ab: Literal["off", "r1_vs_i3"] = "off"


class Paths(Strict):
    """User-visible output folders. Expanded with ``os.path.expandvars`` at use (see ``config.exports_root``)."""

    exports_root: str = r"%USERPROFILE%\DuoSkin Exports"
    tripo_inbox: str = r"%USERPROFILE%\DuoSkin Exports\TripoPacks\inbox"


class Settings(Strict):
    schema_version: int = SETTINGS_SCHEMA_VERSION
    port: int = Field(default=8765, ge=1, le=65535)     # sticky; the last bound port is written back
    demo_mode: bool = False                             # all providers mock + banner; export blocked
    dev_mode: bool = False                              # /api/docs, verbose logs
    models: ModelPins = Field(default_factory=ModelPins)
    budgets: Budgets = Field(default_factory=Budgets)
    providers: ProviderSettings = Field(default_factory=ProviderSettings)
    three_d: ThreeD = Field(default_factory=ThreeD)
    checks: ChecksSettings = Field(default_factory=ChecksSettings)
    paths: Paths = Field(default_factory=Paths)
    planner_structure_lru_hint: bool = False
    capabilities: dict[str, bool | str] = Field(default_factory=dict)   # §7.1 flags from probes and errors
    telemetry: Literal["off"] = "off"                   # nothing leaves the PC except provider calls

    def mode_of(self, provider: str) -> ProviderMode:
        """Effective mode of a provider (demo mode forces everything except disabled providers to mock)."""
        mode = self.providers.modes.get(provider, ProviderMode.DISABLED)
        if self.demo_mode and mode != ProviderMode.DISABLED:
            return ProviderMode.MOCK
        return mode


# Fields the settings API refuses to change (code constants, not user choices).
READ_ONLY_SETTINGS = ("schema_version", "telemetry")


class SettingsPatchError(ValueError):
    pass


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def apply_settings_patch(current: Settings, patch: dict[str, Any]) -> Settings:
    """Deep-merge ``patch`` (a nested partial dict) into ``current`` and re-validate. Raises ``SettingsPatchError``.

    Dict-valued settings (``providers.modes``, ``capabilities``, ``check_overrides``, ``models.candidates``) merge key
    by key; a key set to ``None`` inside ``capabilities``/``check_overrides``/``candidates`` removes it.
    """
    if not isinstance(patch, dict):
        raise SettingsPatchError("settings patch must be a JSON object")
    for key in READ_ONLY_SETTINGS:
        if key in patch and patch[key] != getattr(current, key):
            raise SettingsPatchError(f"'{key}' cannot be changed")
    merged = _deep_merge(current.model_dump(mode="json"), {k: v for k, v in patch.items() if k not in READ_ONLY_SETTINGS})
    for container in (("capabilities",), ("checks", "check_overrides"), ("models", "candidates")):
        node: Any = merged
        for part in container[:-1]:
            node = node[part]
        node[container[-1]] = {k: v for k, v in node[container[-1]].items() if v is not None}
    try:
        return Settings.model_validate(merged)
    except ValueError as exc:   # pydantic.ValidationError subclasses ValueError
        raise SettingsPatchError(str(exc)) from exc
