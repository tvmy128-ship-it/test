"""Keystore (§14.2, CHK-S12) and logging redaction (SYS-01)."""
from __future__ import annotations

import json
import logging
import os

import pytest

from duoskin import config, keystore, logsetup, winplat
from duoskin.keystore import MAX_KEY_LEN, KeyRejected, KeyStore, mask_key
from duoskin.logsetup import RedactFilter, redact, register_secret

ANT = "sk-ant-api03-" + "AbCdEfGh12345678" * 3
OAI = "sk-proj-" + "Zy9x8w7v6u5t4s3r" * 2


class FakeKeyring:
    def __init__(self, fail=False):
        self.store, self.fail = {}, fail

    def get_password(self, service, user):
        if self.fail:
            raise RuntimeError("no backend")
        return self.store.get((service, user))

    def set_password(self, service, user, value):
        if self.fail:
            raise RuntimeError("no backend")
        self.store[(service, user)] = value

    def delete_password(self, service, user):
        if self.fail:
            raise RuntimeError("no backend")
        self.store.pop((service, user), None)


@pytest.fixture
def paths(tmp_path):
    return config.paths(tmp_path / "home")


@pytest.fixture
def ks(paths):
    return KeyStore(paths, keyring_module=FakeKeyring(), environ={})


# ------------------------------------------------------------------------------------------------- keystore
def test_set_get_delete_via_keyring(ks):
    assert ks.get_key("anthropic") is None
    assert ks.set_key("anthropic", f"  {ANT}\n") == "keyring"            # whitespace is stripped
    assert ks.get_key("anthropic") == ANT
    st = ks.status_of("anthropic")
    assert st.set and st.source == "keyring" and st.masked == "sk-\u2026" + ANT[-4:] and st.requirement == "required"
    assert ANT not in st.model_dump_json() and ANT not in json.dumps(ks.key_status(), default=lambda o: o.model_dump())
    ks.delete_key("anthropic")
    assert ks.get_key("anthropic") is None and not ks.status_of("anthropic").set


def test_all_six_providers_with_their_requirement_levels(ks):
    status = ks.key_status()
    assert list(status) == ["anthropic", "openai", "tripo", "recraft", "gemini", "fal"]
    assert [s.requirement for s in status.values()] == ["required", "required", "required", "recommended", "optional", "optional"]
    assert [s.env_var for s in status.values()] == ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TRIPO_API_KEY", "RECRAFT_API_TOKEN",
                                                    "GEMINI_API_KEY", "FAL_KEY"]


@pytest.mark.parametrize("value,fragment", [("", "empty"), ("   ", "empty"), ("x" * (MAX_KEY_LEN + 1), "1280"),
                                            ("abc\ndef", "control"), ("tab\there", "control")])
def test_bad_values_are_rejected(ks, value, fragment):
    with pytest.raises(KeyRejected, match=fragment):
        ks.set_key("openai", value)
    assert ks.get_key("openai") is None


def test_exactly_1280_characters_is_accepted(ks):
    ks.set_key("recraft", "k" * MAX_KEY_LEN)
    assert len(ks.get_key("recraft")) == MAX_KEY_LEN


def test_unknown_provider_is_rejected(ks):
    for fn in (ks.get_key, ks.delete_key, ks.status_of):
        with pytest.raises(KeyRejected):
            fn("nope")
    with pytest.raises(KeyRejected):
        ks.set_key("nope", "abcdefghijkl")


def test_environment_variable_wins_over_the_stores(paths):
    kr = FakeKeyring()
    ks = KeyStore(paths, keyring_module=kr, environ={"OPENAI_API_KEY": OAI})
    ks.set_key("openai", "sk-stored-" + "q" * 20)
    assert ks.get_key("openai") == OAI
    st = ks.status_of("openai")
    assert st.source == "environment" and OAI not in st.model_dump_json()
    ks.delete_key("openai")
    assert ks.get_key("openai") == OAI                                    # deleting the stored copy never touches the environment


def test_falls_back_to_the_file_store_when_keyring_fails(paths):
    ks = KeyStore(paths, keyring_module=FakeKeyring(fail=True), environ={})
    where = ks.set_key("tripo", "tsk_" + "a1b2c3d4" * 4)
    assert where in ("dev_file", "dpapi_file")
    assert ks.get_key("tripo").startswith("tsk_") and ks.status_of("tripo").source == where
    saved = (paths.secrets_dev if where == "dev_file" else paths.secrets_dpapi)
    assert saved.exists()
    if where == "dev_file" and os.name == "posix":
        assert oct(saved.stat().st_mode & 0o777) == "0o600"               # owner only


def test_no_keyring_at_all_uses_the_file_store(paths):
    ks = KeyStore(paths, keyring_module=False, environ={})
    ks.set_key("gemini", "AIza" + "x" * 30)
    assert ks.status_of("gemini").store != "keyring" and ks.get_key("gemini").startswith("AIza")


def test_file_store_with_injected_protection_is_not_plaintext(paths):
    from duoskin.keystore import _FileBackend

    xor = lambda b: bytes(c ^ 0x5A for c in b)
    fb = _FileBackend(paths.secrets_dpapi, xor, xor, "dpapi_file")
    ks = KeyStore(paths, keyring_module=False, environ={}, file_backend=fb)
    secret = "sk-secret-value-123456"
    ks.set_key("openai", secret)
    raw = paths.secrets_dpapi.read_text(encoding="utf-8")
    assert secret not in raw and ks.get_key("openai") == secret and ks.status_of("openai").source == "dpapi_file"
    ks.delete_key("openai")
    assert ks.get_key("openai") is None


def test_keyring_write_replaces_a_stale_fallback_copy(paths):
    kr = FakeKeyring(fail=True)
    ks = KeyStore(paths, keyring_module=kr, environ={})
    ks.set_key("openai", "sk-old-" + "o" * 20)                            # lands in the file
    kr.fail = False
    ks.set_key("openai", "sk-new-" + "n" * 20)                            # lands in the keyring and clears the file copy
    assert ks.get_key("openai").startswith("sk-new-") and ks._file.get("openai") is None


def test_test_results_are_remembered_without_secrets(ks):
    ks.set_key("recraft", "r" * 30)
    ks.record_test("recraft", True, "ok")
    ks.record_test("openai", False, "401 from provider")
    assert ks.status_of("recraft").last_test.ok is True and ks.status_of("openai").last_test.message == "401 from provider"


@pytest.mark.parametrize("secret,expected", [(ANT, "sk-\u2026" + ANT[-4:]), ("tsk_" + "z" * 20, "tsk_\u2026" + "zzzz"),
                                             ("AIza" + "q" * 30, "AIza\u2026qqqq"), ("short", "..."),
                                             ("plain-key-without-prefix", "\u2026fix"[0:0] + "\u2026" + "efix"[-4:])])
def test_mask_key(secret, expected):
    assert mask_key(secret) == expected
    assert secret not in mask_key(secret) or secret == "short"


def test_module_level_api_uses_the_configured_store(ks):
    keystore.configure(ks)
    try:
        keystore.set_key("openai", OAI)
        assert keystore.get_key("openai") == OAI and keystore.key_status()["openai"].set
        keystore.delete_key("openai")
        assert keystore.get_key("openai") is None
    finally:
        keystore.configure(None)


def test_reading_a_key_registers_it_with_the_log_redactor(ks):
    ks.set_key("fal", "fal-key-" + "m1n2o3p4" * 3)
    value = ks.get_key("fal")
    assert "[REDACTED]" in redact(f"calling with {value} now") and value not in redact(f"calling with {value} now")


# ------------------------------------------------------------------------------------------------- redaction
@pytest.mark.parametrize("text", [
    f"key={ANT}", f"Authorization: Bearer {'a' * 30}", "x-api-key: abcdef123456", "tsk_" + "A1b2C3d4" * 3,
    "AIza" + "SyD-9tSrke72PouQMnMX-a7eZSW0jkFMBWY", f"api_key = '{OAI}'",
    "https://files.example/x.glb?X-Amz-Signature=deadbeefcafe&other=1", f"{{'api_key': '{OAI}'}}",
])
def test_redact_masks_every_known_secret_shape(text):
    out = redact(text)
    assert "[REDACTED]" in out
    for frag in (ANT, OAI, "deadbeefcafe", "abcdef123456", "SyD-9tSrke72PouQMnMX"):
        assert frag not in out


def test_redact_leaves_ordinary_text_alone():
    assert redact("step stp_01 finished in 3.2s (cached)") == "step stp_01 finished in 3.2s (cached)"
    assert redact("") == ""


def test_exact_registered_values_are_masked_even_without_a_known_shape():
    secret = "plain-recraft-token-9f8e7d6c5b"
    register_secret(secret)
    assert redact(f"token is {secret}!") == "token is [REDACTED]!"
    register_secret("short")                                              # too short to register safely
    assert "short" in redact("a short sentence")


def test_filter_masks_message_args_and_tracebacks():
    logger = logging.getLogger("duoskin.test_redact")
    logger.propagate = False
    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(logsetup.JsonFormatter().format(record))

    h = Capture()
    h.addFilter(RedactFilter())
    logger.addHandler(h)
    logger.setLevel(logging.DEBUG)
    try:
        logger.info("calling %s with %s", "openai", ANT)
        try:
            raise RuntimeError(f"401 for key {OAI}")
        except RuntimeError:
            logger.exception("request failed")
    finally:
        logger.removeHandler(h)
    blob = "\n".join(records)
    assert ANT not in blob and OAI not in blob and "[REDACTED]" in blob and "request failed" in blob


def test_setup_logging_writes_json_lines_without_keys(tmp_path):
    handle = logsetup.setup_logging(tmp_path / "logs", console=False, hooks=False)
    try:
        log = logging.getLogger("duoskin.test_setup")
        log.warning("hello %s", ANT, extra={"step_id": "stp_1"})
        for h in logging.getLogger().handlers:
            h.flush()
    finally:
        handle.shutdown()
    lines = (tmp_path / "logs" / "duoskin.log").read_text(encoding="utf-8").strip().splitlines()
    rec = json.loads(lines[-1])
    assert rec["level"] == "WARNING" and rec["logger"] == "duoskin.test_setup" and rec["step_id"] == "stp_1"
    assert ANT not in lines[-1] and "[REDACTED]" in rec["msg"] and rec["ts"].endswith("Z")


def test_setup_logging_is_idempotent_and_silences_sdk_debug(tmp_path):
    h1 = logsetup.setup_logging(tmp_path / "a", console=False, hooks=False)
    h2 = logsetup.setup_logging(tmp_path / "b", console=False, hooks=False)
    try:
        ours = [h for h in logging.getLogger().handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert len(ours) == 1 and "b" in ours[0].baseFilename
        assert logging.getLogger("httpx").level >= logging.WARNING and logging.getLogger("anthropic").level >= logging.WARNING
    finally:
        h2.shutdown()
        h1.shutdown()


def test_asyncio_winerror_10054_noise_is_dropped(tmp_path):
    handle = logsetup.setup_logging(tmp_path / "logs", console=False, hooks=False)
    try:
        logging.getLogger("asyncio").error("Exception in callback: ConnectionResetError: [WinError 10054] reset")
        logging.getLogger("duoskin.x").error("a real error mentioning ConnectionResetError")
        for h in logging.getLogger().handlers:
            h.flush()
    finally:
        handle.shutdown()
    text = (tmp_path / "logs" / "duoskin.log").read_text(encoding="utf-8")
    assert "WinError 10054" not in text and "a real error" in text


def test_dpapi_is_unavailable_off_windows():
    if winplat.IS_WINDOWS:
        pytest.skip("Windows")
    with pytest.raises(winplat.DpapiUnavailable):
        winplat.dpapi_protect(b"x")
