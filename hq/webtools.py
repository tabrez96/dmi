"""HQ's handle on the shared web tools (tools/_web.py), used by the gate."""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("dmi_web", Path(__file__).resolve().parents[1] / "tools" / "_web.py")
_web = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_web)

WebError = _web.WebError
search = _web.search
fetch = _web.fetch
