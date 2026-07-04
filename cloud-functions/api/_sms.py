"""On-call SMS channel, inbound half (ARCHITECTURE §6A) — pure helpers.

The whole reply grammar: `YES [incident-id]` / `NO [incident-id]`,
case-insensitive. A bare YES/NO is accepted only when exactly one approval is
pending across incidents; anything else replies with the grammar. Kept free of
I/O so tests exercise every branch directly.
"""

import base64
import hashlib
import hmac

GRAMMAR_HELP = 'Reply "YES <incident-id>" or "NO <incident-id>" (or bare YES/NO when one approval is pending).'


def parse_reply(body_text, pending_ids):
    """Parse an inbound SMS against the pending approvals.

    Returns (approved, incident_id, reply) — approved is True/False when the
    message resolves to a decision (incident_id set), else None and `reply`
    explains what to send instead.
    """
    tokens = (body_text or "").strip().split()
    if not tokens:
        return None, None, GRAMMAR_HELP
    word = tokens[0].strip(".,!?").upper()
    if word not in ("YES", "NO"):
        return None, None, GRAMMAR_HELP
    approved = word == "YES"

    rest = " ".join(tokens[1:]).strip().strip(".,!?")
    if rest:
        match = next((i for i in pending_ids if i.lower() == rest.lower()), None)
        if match is None:
            return None, None, f"No approval pending for '{rest}'. Pending: {', '.join(pending_ids) or 'none'}."
        return approved, match, None

    if len(pending_ids) == 1:
        return approved, pending_ids[0], None
    if not pending_ids:
        return None, None, "No approvals are pending."
    return None, None, f"Multiple approvals pending — specify one: {', '.join(pending_ids)}."


def validate_twilio_signature(url, params, signature, auth_token):
    """Twilio's scheme: HMAC-SHA1 over the full URL + params concatenated in
    sorted-key order, base64-encoded, compared against X-Twilio-Signature."""
    if not auth_token or not signature:
        return False
    payload = url + "".join(k + v for k, v in sorted(params.items()))
    digest = base64.b64encode(
        hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    ).decode()
    return hmac.compare_digest(digest, signature)


def twiml(message):
    """Minimal TwiML so Twilio texts `message` back to the sender."""
    escaped = (
        message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{escaped}</Message></Response>'
