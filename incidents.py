"""Load the labeled incident scenarios and per-pack assets.

Key rule: agents may see `alert` + `telemetry`. They must NEVER see `ground_truth` —
that block exists only for the eval harness (Phase 5) to score a run.

Two packs ship (ARCHITECTURE §6): `it-ops` (default) and `sec-ops`. A pack is just a
directory read here — no registry, no plugin framework (§6 hard rule). `sec-ops` adds a
real Diagnosis tool in `tools.py`, loaded by file path because the dir name is not a
valid module.
"""
import importlib.util
import json
from pathlib import Path
from types import ModuleType

_PACKS_DIR = Path(__file__).parent / "packs"
DEFAULT_PACK = "it-ops"
# The packs that exist as directories. Kept explicit (not globbed) so an unfinished
# pack folder can't silently join the demo.
PACKS = ("it-ops", "sec-ops")


def load_incidents(pack: str = DEFAULT_PACK) -> list[dict]:
    data = json.loads((_PACKS_DIR / pack / "scenarios.json").read_text())
    return data["incidents"]


def load_rubric(pack: str = DEFAULT_PACK) -> str:
    return (_PACKS_DIR / pack / "rubric.md").read_text().strip()


def find_incident(incident_id: str) -> tuple[dict, str]:
    """Return (incident, pack_name) by searching every pack. Ids are unique across packs."""
    for pack in PACKS:
        for inc in load_incidents(pack):
            if inc["id"] == incident_id:
                return inc, pack
    raise KeyError(f"No incident with id {incident_id!r}")


def get_incident(incident_id: str) -> dict:
    return find_incident(incident_id)[0]


def pack_of(incident_id: str) -> str:
    return find_incident(incident_id)[1]


def observable(incident: dict) -> dict:
    """The slice an agent is allowed to see: alert + telemetry, no ground truth."""
    return {"alert": incident["alert"], "telemetry": incident["telemetry"]}


_TOOLS_CACHE: dict[str, ModuleType | None] = {}


def load_pack_tools(pack: str) -> ModuleType | None:
    """Import packs/<pack>/tools.py by file path (the dir name isn't importable).
    Loaded once per pack. Returns None when the pack has no tools (e.g. it-ops)."""
    if pack not in _TOOLS_CACHE:
        tools_path = _PACKS_DIR / pack / "tools.py"
        if not tools_path.exists():
            _TOOLS_CACHE[pack] = None
        else:
            spec = importlib.util.spec_from_file_location(
                f"packs_{pack.replace('-', '_')}_tools", tools_path
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _TOOLS_CACHE[pack] = module
    return _TOOLS_CACHE[pack]
