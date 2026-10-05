"""Exit 0 when this interpreter is the build DuoSkin Studio supports (APP_SPEC section 15.1): 64-bit x64, GIL build, real
install (not the Microsoft Store stub), CPython 3.14 or 3.13. Used by setup.bat."""
import sys
import sysconfig

ok = (sysconfig.get_platform() == "win-amd64"
      and not sysconfig.get_config_var("Py_GIL_DISABLED")
      and "windowsapps" not in sys.base_prefix.lower()
      and sys.version_info[:2] in ((3, 14), (3, 13)))
sys.exit(0 if ok else 1)
