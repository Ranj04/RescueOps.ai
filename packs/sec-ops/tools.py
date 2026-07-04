"""sec-ops pack tools — Phase A6a.

The ONE real external tool the Diagnosis stage gains for this pack: a live CVE lookup
against the NVD 2.0 API (CVSS + description) and the CISA Known-Exploited-Vulnerabilities
catalog. Responses are cached in-process. The tool NEVER raises: if the feed is killed by
chaos (`feed_killed=True`) or is unreachable, it returns a `degraded=True` result so the
pipeline can lower confidence and keep going — the graceful-degradation demo beat.

Loaded by file path (the pack dir "sec-ops" is not a valid module name); see
incidents.load_pack_tools. `fetch` is injectable so tests stay deterministic and offline.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Any, Callable

# NVD single-CVE query and the CISA KEV catalog. The CVE id is appended url-encoded,
# and only after it matches the canonical CVE pattern below — never interpolated raw.
_NVD_BASE = "https://services.nvd.nist.gov/rest/json/cves/2.0?cveId="
_KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
_CVE_RE = re.compile(r"^CVE-\d{4}-\d{4,}$")

_CVE_CACHE: dict[str, dict[str, Any]] = {}
_KEV_CACHE: dict[str, set[str] | None] = {"ids": None}


def _http_get_json(url: str, timeout: float = 8.0) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "RescueOps-secops/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (https only)
        return json.loads(response.read().decode("utf-8"))


def _parse_nvd(cve_id: str, payload: Any) -> dict[str, Any]:
    vulns = payload.get("vulnerabilities") or []
    if not vulns:
        raise ValueError(f"NVD returned no record for {cve_id}")
    cve = vulns[0]["cve"]
    descriptions = cve.get("descriptions") or []
    description = next(
        (d["value"] for d in descriptions if d.get("lang") == "en"),
        (descriptions[0]["value"] if descriptions else ""),
    )
    metrics = cve.get("metrics") or {}
    score, severity = None, None
    for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        entries = metrics.get(key)
        if entries:
            data = entries[0]["cvssData"]
            score = data.get("baseScore")
            severity = data.get("baseSeverity") or entries[0].get("baseSeverity")
            break
    return {"cve_id": cve_id, "cvss_score": score, "cvss_severity": severity,
            "description": description}


def _in_kev(cve_id: str, fetch: Callable[[str], Any]) -> bool:
    if _KEV_CACHE["ids"] is None:
        catalog = fetch(_KEV_URL)
        _KEV_CACHE["ids"] = {v["cveID"].upper() for v in catalog.get("vulnerabilities", [])}
    return cve_id in _KEV_CACHE["ids"]


def _degraded(cve_id: str, reason: str) -> dict[str, Any]:
    return {"cve_id": cve_id, "cvss_score": None, "cvss_severity": None,
            "description": "", "known_exploited": None, "degraded": True,
            "cached": False, "reason": reason}


def lookup_cve(
    cve_id: str,
    *,
    feed_killed: bool = False,
    fetch: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Return CVE facts as a dict. Always returns; never raises. On chaos-kill, a
    malformed id, or any feed error the result carries `degraded=True` with
    `cvss_score`/`known_exploited` unknown (None)."""
    cve_id = cve_id.upper().strip()
    if not _CVE_RE.match(cve_id):
        return _degraded(cve_id, "not a valid CVE identifier")
    if cve_id in _CVE_CACHE:
        return {**_CVE_CACHE[cve_id], "cached": True}
    if feed_killed:
        return _degraded(cve_id, "CVE feed disabled by chaos")

    fetch = fetch or _http_get_json
    try:
        url = _NVD_BASE + urllib.parse.quote(cve_id)
        result = _parse_nvd(cve_id, fetch(url))
        result["known_exploited"] = _in_kev(cve_id, fetch)
        result["degraded"] = False
        _CVE_CACHE[cve_id] = result
        return {**result, "cached": False}
    except Exception as error:  # noqa: BLE001 — degrade on ANY feed failure, never crash
        return _degraded(cve_id, f"{type(error).__name__}: {error}")


def reset_cache() -> None:
    """Clear the in-process caches (tests)."""
    _CVE_CACHE.clear()
    _KEV_CACHE["ids"] = None
