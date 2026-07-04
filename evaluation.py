"""Eval harness — score the pipeline against labeled ground truth.

This is the "measured performance, not vibes" demo moment. It is the ONLY place
allowed to read a pack's `ground_truth`: the live run never sees it.
Runs incidents with no chaos + an auto-approve callback and scores each artifact
against ground truth. Persistence is NOT here: the cached summary lives in the
platform store under `eval:{pack}` (written by POST /api/eval), per the
native-first rule — this module stays pure scoring so it can run anywhere.

Confidence is computed by Track A at runtime (we display it, never recompute it).
"""
from datetime import datetime, timezone

import incidents
from pipeline import run_incident
from schemas import ApprovalDecision, RemediationPlan

# A terse ground-truth phrase counts as "covered" by a produced action when at
# least this fraction of its salient words appear in that action. Directional
# (coverage of EXPECTED by PRODUCED), so a short canonical label still matches a
# verbose produced action — symmetric Jaccard over the union would score ~0.
_COVERAGE_THRESHOLD = 0.5

# Generic filler dropped before comparison so only content words count.
_STOPWORDS = {
    "the", "a", "an", "to", "of", "on", "in", "and", "or", "for", "with", "via",
    "by", "from", "then", "if", "is", "are", "be", "that", "this", "it", "its",
    "as", "at", "back", "old", "new", "any", "all", "one",
}


def _auto_approve(plan: RemediationPlan) -> ApprovalDecision:
    return ApprovalDecision(approved=True, approver="auto-eval", note="auto-approved for eval")


def _salient_words(text: str) -> set[str]:
    """Content words of `text`: lowercased alnum tokens >2 chars, minus stopwords."""
    words = "".join(c.lower() if c.isalnum() else " " for c in text).split()
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def _word_found(expected_word: str, produced_words: set[str]) -> bool:
    """An expected word is present if it matches exactly, or (for words >=4 chars)
    one produced word is a morphological variant — substring either way, e.g.
    deploy/deployment, postgres/postgresql, roll(back), concurrency."""
    if expected_word in produced_words:
        return True
    if len(expected_word) >= 4:
        return any(expected_word in pw or pw in expected_word for pw in produced_words)
    return False


def _fuzzy_match(expected: str, produced: str) -> bool:
    """True if `produced` covers `expected` — substring either way, or at least
    _COVERAGE_THRESHOLD of `expected`'s salient words appear in `produced`."""
    e, p = expected.lower().strip(), produced.lower().strip()
    if not e or not p:
        return False
    if e in p or p in e:
        return True
    we = _salient_words(expected)
    if not we:
        return False
    wp = _salient_words(produced)
    coverage = sum(_word_found(w, wp) for w in we) / len(we)
    return coverage >= _COVERAGE_THRESHOLD


def _recall(expected: list[str], produced: list[str]) -> float:
    """Fraction of expected items matched by at least one produced item."""
    if not expected:
        return 1.0
    matched = sum(1 for e in expected if any(_fuzzy_match(e, p) for p in produced))
    return matched / len(expected)


def score_incident(incident: dict) -> dict:
    incident_id = incident["id"]
    gt = incident["ground_truth"]
    result = run_incident(incident_id, approval_callback=_auto_approve)

    severity_correct = result.triage.severity == gt["severity"]

    evidence_recall = _recall(gt["expected_evidence"], result.diagnosis.cited_evidence)

    expected_actions = gt["remediation"]["safe"] + gt["remediation"]["risky"]
    produced_actions = [a.action for a in result.remediation.safe] + [
        a.action for a in result.remediation.risky
    ]
    remediation_overlap = _recall(expected_actions, produced_actions)

    return {
        "incident_id": incident_id,
        "severity_correct": severity_correct,
        "evidence_recall": round(evidence_recall, 3),
        "remediation_overlap": round(remediation_overlap, 3),
        "recovered": bool(result.verification.recovered),
        "confidence": round(float(result.diagnosis.confidence), 3),
    }


def aggregate(by_incident: list[dict]) -> dict:
    n = len(by_incident) or 1
    return {
        "severity_accuracy": round(sum(r["severity_correct"] for r in by_incident) / n, 3),
        "mean_evidence_recall": round(sum(r["evidence_recall"] for r in by_incident) / n, 3),
        "mean_remediation_overlap": round(sum(r["remediation_overlap"] for r in by_incident) / n, 3),
        "recovery_rate": round(sum(r["recovered"] for r in by_incident) / n, 3),
    }


def summarize(pack: str, by_incident: list[dict]) -> dict:
    """Assemble the eval summary cached under `eval:{pack}` and rendered by the board."""
    return {
        "pack": pack,
        "incidents_run": len(by_incident),
        "by_incident": by_incident,
        "aggregate": aggregate(by_incident),
        "scored_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def evaluate_pack(pack: str = incidents.DEFAULT_PACK) -> dict:
    """Run + score every labeled incident in the pack. Local/CLI convenience;
    on prod the eval agent scores one incident per invocation to stay inside
    runtime limits and this module only supplies the pieces."""
    by_incident = [score_incident(inc) for inc in incidents.load_incidents(pack)]
    return summarize(pack, by_incident)
