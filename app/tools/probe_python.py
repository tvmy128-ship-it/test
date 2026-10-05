"""Exit 0 when this interpreter is a build DuoSkin Studio supports (APP_SPEC section 15.1): 64-bit x64, GIL build, real
install (not the Microsoft Store stub), CPython 3.14, 3.13 or 3.12. Used by setup.bat.

3.12 is accepted as a last resort: requirements\\win-x64.lock has wheels for cp312, cp313 and cp314 (tools/check_lock.py
--online verifies it), and 3.12.10 is the last 3.12 build with a Windows installer. 3.11 and older, 3.15+, 32-bit, ARM64
and free-threaded builds are refused."""
import sys
import sysconfig

SUPPORTED = ((3, 14), (3, 13), (3, 12))



def is_supported() -> bool:
    return (sysconfig.get_platform() == "win-amd64"                 # x64 only (no 32-bit, no ARM64 build)
            and not sysconfig.get_config_var("Py_GIL_DISABLED")     # no free-threaded 3.14t
            and "windowsapps" not in sys.base_prefix.lower()        # not the Store stub or an install-manager alias
            and sys.version_info[:2] in SUPPORTED)


if __name__ == "__main__":
    sys.exit(0 if is_supported() else 1)
