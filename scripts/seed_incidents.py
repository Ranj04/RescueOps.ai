"""Track B demo tooling — seed incident summaries into prod storage.

Reads packs/<pack>/scenarios.json (Track A data, read-only here), strips
telemetry and ground_truth (the surface never sees ground truth), and POSTs
the summaries to /api/incidents so storage is the system of record.

Usage:  python3 scripts/seed_incidents.py [base_url]
"""

import json
import sys
import urllib.request
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "https://rescueops-dpj9utykdvs3.edgeone.dev"
PACKS_DIR = Path(__file__).resolve().parent.parent / "packs"

for pack_dir in sorted(PACKS_DIR.iterdir()):
    scenarios = pack_dir / "scenarios.json"
    if not scenarios.is_file():
        continue
    data = json.loads(scenarios.read_text())
    summaries = [
        {"id": inc["id"], "title": inc.get("title", ""), "alert": inc.get("alert", "")}
        for inc in data["incidents"]
    ]
    body = json.dumps({"pack": pack_dir.name, "incidents": summaries}).encode()
    req = urllib.request.Request(
        f"{BASE}/api/incidents", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        print(pack_dir.name, res.status, res.read().decode()[:200])
