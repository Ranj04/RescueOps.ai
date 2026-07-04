"""B4a §6A: the SMS channel — grammar parser, signature validation, and the
one-approval-writer guarantee (web racing SMS yields exactly one event).

Cloud-function modules are loaded by file path (underscore-prefixed files are
deliberately not importable as packages)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import sys
from pathlib import Path

_CF_API = Path(__file__).parents[1] / "cloud-functions" / "api"
_CF = Path(__file__).parents[1] / "cloud-functions"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # api/index.py does `from _sms import ...`
    spec.loader.exec_module(module)
    return module

_storage = _load("_storage", _CF / "_storage.py")
_sms = _load("_sms", _CF_API / "_sms.py")
sys.path.insert(0, str(_CF_API))
sys.path.insert(0, str(_CF))
api_index = _load("api_index", _CF_API / "index.py")

import notify  # noqa: E402  (repo root, stdlib-only)


# ---------------------------------------------------------------------------
# Grammar parser — the WHOLE grammar per §6A.
# ---------------------------------------------------------------------------
PENDING_ONE = ["INC-001-checkout-db-pool"]
PENDING_TWO = ["INC-001-checkout-db-pool", "SEC-001-log4shell-jndi"]


def test_yes_with_id_case_insensitive():
    approved, incident, reply = _sms.parse_reply("yes inc-001-CHECKOUT-db-pool", PENDING_TWO)
    assert approved is True and incident == "INC-001-checkout-db-pool" and reply is None


def test_no_with_id():
    approved, incident, reply = _sms.parse_reply("NO SEC-001-log4shell-jndi", PENDING_TWO)
    assert approved is False and incident == "SEC-001-log4shell-jndi" and reply is None


def test_bare_yes_single_pending():
    approved, incident, reply = _sms.parse_reply("YES", PENDING_ONE)
    assert approved is True and incident == PENDING_ONE[0] and reply is None


def test_bare_no_single_pending_with_punctuation():
    approved, incident, reply = _sms.parse_reply("  no!  ", PENDING_ONE)
    assert approved is False and incident == PENDING_ONE[0] and reply is None


def test_bare_yes_multiple_pending_asks_to_specify():
    approved, incident, reply = _sms.parse_reply("YES", PENDING_TWO)
    assert incident is None and "specify" in reply.lower()
    assert all(i in reply for i in PENDING_TWO)


def test_bare_yes_nothing_pending():
    approved, incident, reply = _sms.parse_reply("YES", [])
    assert incident is None and "no approvals" in reply.lower()


def test_unknown_id_lists_pending():
    approved, incident, reply = _sms.parse_reply("YES INC-999", PENDING_ONE)
    assert incident is None and "INC-999" in reply and PENDING_ONE[0] in reply


def test_garbage_gets_grammar_help():
    for garbage in ("maybe", "approve it", "", "🙂", "yes-ish"):
        approved, incident, reply = _sms.parse_reply(garbage, PENDING_ONE)
        assert incident is None and reply == _sms.GRAMMAR_HELP


# ---------------------------------------------------------------------------
# Twilio signature validation.
# ---------------------------------------------------------------------------
def _sign(url: str, params: dict, token: str) -> str:
    payload = url + "".join(k + v for k, v in sorted(params.items()))
    return base64.b64encode(
        hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    ).decode()


def test_signature_roundtrip():
    url = "https://rescueops-hq.edgeone.cool/api/sms/inbound"
    params = {"From": "+15550001111", "Body": "YES"}
    sig = _sign(url, params, "token123")
    assert _sms.validate_twilio_signature(url, params, sig, "token123")
    assert not _sms.validate_twilio_signature(url, params, sig, "other-token")
    assert not _sms.validate_twilio_signature(url, {**params, "Body": "NO"}, sig, "token123")
    assert not _sms.validate_twilio_signature(url, params, "", "token123")
    assert not _sms.validate_twilio_signature(url, params, sig, "")


# ---------------------------------------------------------------------------
# One approval writer: web racing SMS yields exactly ONE approval event.
# ---------------------------------------------------------------------------
class _FakeStore:
    """Sync stand-in for the cloud-function-side agent.store facade."""

    def __init__(self):
        self.kv: dict[str, str] = {}
        self.messages: dict[str, list[dict]] = {}

    def get(self, key):
        return self.kv.get(key)

    def put(self, key, value):
        self.kv[key] = value

    def delete_key(self, key):
        self.kv.pop(key, None)

    def append_message(self, cid, role, content):
        self.messages.setdefault(cid, []).append({"role": role, "content": content})

    def get_messages(self, cid, limit=100, order="asc"):
        items = list(self.messages.get(cid, []))
        if order == "desc":
            items.reverse()
        return items[:limit]


def _storage_with_pending(incident_id="INC-001-checkout-db-pool"):
    storage = _storage.Storage(_FakeStore())
    storage.put_json("packs", ["it-ops"])
    storage.put_json("incidents:it-ops", [{"id": incident_id}])
    storage.append_event(incident_id, "system", "incident_opened",
                         {"summary": "Checkout is down."})
    storage.append_event(incident_id, "system", "approval_requested",
                         {"summary": "Risky remediation is waiting for human approval."})
    return storage


def test_double_approval_web_races_sms_exactly_one_event():
    storage = _storage_with_pending()
    incident = "INC-001-checkout-db-pool"

    web = api_index.decide_approval(storage, incident, True, channel="web", approver="human-ui")
    sms = api_index.decide_approval(storage, incident, True, channel="sms", approver="+15550001111")

    assert web is not None and web["payload"]["channel"] == "web"
    assert sms is None  # race lost — "already resolved"
    decisions = [e for e in storage.read_events(incident)
                 if e["type"] in ("approval_granted", "approval_denied")]
    assert len(decisions) == 1


def test_pending_approvals_scans_across_packs():
    storage = _storage_with_pending()
    assert api_index.pending_approvals(storage) == ["INC-001-checkout-db-pool"]
    api_index.decide_approval(storage, "INC-001-checkout-db-pool", False,
                              channel="web", approver="human-ui")
    assert api_index.pending_approvals(storage) == []


# ---------------------------------------------------------------------------
# Outbound notify: disabled by default; composes one-segment bodies.
# ---------------------------------------------------------------------------
def test_notify_disabled_without_env(monkeypatch):
    for var in ("SMS_ENABLED", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
                "TWILIO_FROM_NUMBER", "ONCALL_PHONE_NUMBER"):
        monkeypatch.delenv(var, raising=False)
    assert notify.enabled() is False
    assert notify.dispatch(
        [{"type": "approval_requested", "payload": {"summary": "s"}}], "INC-001"
    ) == []


def test_notify_dispatch_sends_and_emits(monkeypatch):
    monkeypatch.setenv("SMS_ENABLED", "true")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC0")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "t")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15550002222")
    monkeypatch.setenv("ONCALL_PHONE_NUMBER", "+15550001111")
    sent = []
    monkeypatch.setattr(notify, "send_sms", lambda body: sent.append(body) or True)

    events = [
        {"type": "approval_requested", "payload": {"summary": "waiting"}},
        {"type": "postmortem_ready", "payload": {"summary": "Root cause: pool exhaustion."}},
        {"type": "incident_resolved", "payload": {"summary": "Resolved."}},
    ]
    emitted = notify.dispatch(events, "INC-001", ["Roll back the deploy"])

    assert len(sent) == 2  # one approval text, one resolution text (postmortem wins)
    assert "YES INC-001" in sent[0] and "Roll back the deploy" in sent[0]
    assert "RESOLVED" in sent[1] and "pool exhaustion" in sent[1]
    assert all(len(b) <= 300 for b in sent)
    assert [e["type"] for e in emitted] == ["oncall_notified", "oncall_notified"]
    assert all(e["payload"]["summary"] for e in emitted)


def test_notify_never_raises_even_if_send_explodes(monkeypatch):
    monkeypatch.setenv("SMS_ENABLED", "true")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC0")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "t")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15550002222")
    monkeypatch.setenv("ONCALL_PHONE_NUMBER", "+15550001111")

    def boom(body):
        raise RuntimeError("twilio is down")

    monkeypatch.setattr(notify, "send_sms", boom)
    assert notify.dispatch(
        [{"type": "approval_requested", "payload": {"summary": "s"}}], "INC-001"
    ) == []
