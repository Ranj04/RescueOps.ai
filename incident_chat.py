"""Incident-chat agent logic — Phase A6b (stretch).

Answers questions about a COMPLETED incident by grounding on its event log and citing
entries by `seq`. No new autonomy: it reads the event log and reasons — it never drives
the pipeline or takes an action. Every LLM call goes through llm_client (the gateway).

Citations are validated against the real event log after the model answers, so a
hallucinated seq is surfaced (`invalid_citations`) rather than trusted.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from llm_client import build_llm


class ChatAnswer(BaseModel):
    answer: str = Field(description="A concise answer grounded only in the event log")
    cited_seqs: list[int] = Field(
        default_factory=list,
        description="The `seq` numbers of the events the answer relies on",
    )


def _event_digest(events: list[dict]) -> str:
    """One line per event: the scoped context the chat agent is allowed to reason over."""
    return "\n".join(
        f"[seq {e['seq']}] {e['actor']}/{e['type']}: {e['payload'].get('summary', '')}"
        for e in events
    )


def _history_block(history: list[dict]) -> str:
    if not history:
        return ""
    lines = "\n".join(f"{turn['role']}: {turn['content']}" for turn in history)
    return f"\nEARLIER IN THIS CONVERSATION:\n{lines}\n"


def _chat_prompt(incident_id: str, question: str, events: list[dict], history: list[dict]) -> str:
    return (
        f"You are answering questions about incident {incident_id}, which has already "
        "concluded. Answer ONLY from the event log below — do not invent facts. Cite the "
        "specific events you rely on by their seq number, and list those seqs in "
        "cited_seqs.\n"
        f"{_history_block(history)}\n"
        f"EVENT LOG:\n{_event_digest(events)}\n\n"
        f"QUESTION: {question}"
    )


def _answer(prompt: str) -> ChatAnswer | None:
    """The single LLM call (through the gateway). Isolated so tests can script it."""
    return build_llm(temperature=0.1).call(prompt, response_model=ChatAnswer)


def answer_about_incident(
    incident_id: str,
    question: str,
    events: list[dict],
    history: list[dict] | None = None,
) -> dict:
    """Answer one question, returning the answer plus which citations are real."""
    prompt = _chat_prompt(incident_id, question, events, history or [])
    answer = _answer(prompt) or ChatAnswer(answer="(could not parse a grounded answer)")
    known_seqs = {e["seq"] for e in events}
    valid = [s for s in answer.cited_seqs if s in known_seqs]
    invalid = [s for s in answer.cited_seqs if s not in known_seqs]
    return {
        "answer": answer.answer,
        "cited_seqs": answer.cited_seqs,
        "valid_citations": valid,
        "invalid_citations": invalid,
    }
