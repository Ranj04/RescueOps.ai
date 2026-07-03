"""EdgeOne Makers model gateway client with one guarded fallback attempt."""

from __future__ import annotations

import os
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import instructor
from crewai import LLM
from dotenv import load_dotenv
from litellm import acompletion, completion
from pydantic import PrivateAttr

from events import append_event

load_dotenv()


class LLMConfigurationError(RuntimeError):
    """Raised when the Makers gateway configuration is incomplete."""


@dataclass
class _RunState:
    incident_id: str
    fallback_active: bool = False
    force_primary_failure: bool = False


_run_state: ContextVar[_RunState | None] = ContextVar(
    "rescueops_model_run_state",
    default=None,
)


def begin_model_run(incident_id: str, force_primary_failure: bool = False) -> None:
    """Start an incident-scoped fallback circuit shared by its specialist models."""
    _run_state.set(
        _RunState(
            incident_id=incident_id,
            force_primary_failure=force_primary_failure,
        )
    )


def _required_setting(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise LLMConfigurationError(
            f"Missing {name}; copy .env.template to .env and configure the Makers gateway"
        )
    return value


def _litellm_model(model: str) -> str:
    return model if model.startswith("openai/") else f"openai/{model}"


def _as_messages(prompt: Any) -> list[dict[str, str]]:
    if isinstance(prompt, str):
        return [{"role": "user", "content": prompt}]
    return list(prompt)


def _structured_kwargs(llm: LLM, response_model: type, messages: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": llm.model,
        "messages": _as_messages(messages),
        "response_model": response_model,
        "api_key": llm.api_key,
        "api_base": getattr(llm, "base_url", None),
        "max_retries": 2,
    }
    temperature = getattr(llm, "temperature", None)
    if temperature is not None:
        kwargs["temperature"] = temperature
    return kwargs


# Structured outputs use instructor's JSON mode (schema in prompt +
# response_format json_object), NOT its default TOOLS mode: the gateway's
# thinking-mode models reject forced tool_choice (deepseek 400s, minimax
# ignores it), while JSON mode was verified against both.
def _structured_completion(llm: LLM, response_model: type, messages: Any) -> Any:
    client = instructor.from_litellm(completion, mode=instructor.Mode.JSON)
    return client.chat.completions.create(**_structured_kwargs(llm, response_model, messages))


async def _astructured_completion(llm: LLM, response_model: type, messages: Any) -> Any:
    client = instructor.from_litellm(acompletion, mode=instructor.Mode.JSON)
    return await client.chat.completions.create(**_structured_kwargs(llm, response_model, messages))


class GatewayLLM(LLM):
    """CrewAI LLM that switches a whole incident to fallback after one failure."""

    _fallback_llm: LLM = PrivateAttr()
    _primary_name: str = PrivateAttr()
    _fallback_name: str = PrivateAttr()
    _state: _RunState = PrivateAttr()

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        primary_model: str,
        fallback_model: str,
        temperature: float,
        state: _RunState,
    ) -> None:
        shared = {
            "base_url": base_url,
            "api_base": base_url,
            "api_key": api_key,
            "temperature": temperature,
            "max_retries": 0,
        }
        super().__init__(model=model, **shared)
        self._fallback_llm = LLM(
            model=_litellm_model(fallback_model),
            **shared,
        )
        self._primary_name = primary_model
        self._fallback_name = fallback_model
        self._state = state

    def _activate_fallback(self, error: Exception) -> None:
        if self._state.fallback_active:
            return
        self._state.fallback_active = True
        append_event(
            incident_id=self._state.incident_id,
            actor="gateway",
            event_type="model_fallback",
            payload={
                "summary": (
                    f"Primary model failed; switched to {self._fallback_name}."
                ),
                "primary_model": self._primary_name,
                "fallback_model": self._fallback_name,
                "error_type": type(error).__name__,
            },
        )

    def _primary_is_forced_down(self) -> bool:
        if self._state.force_primary_failure and not self._state.fallback_active:
            self._state.force_primary_failure = False
            return True
        return False

    @staticmethod
    def _pop_structured_request(args: tuple, kwargs: dict) -> tuple[type | None, Any]:
        """Intercept crewai's response_model so structured outputs run through
        our instructor JSON-mode path with explicit gateway credentials, instead
        of crewai's InternalInstructor (which calls litellm bare, in TOOLS mode)."""
        response_model = kwargs.pop("response_model", None)
        messages = args[0] if args else kwargs.get("messages")
        return response_model, messages

    def call(self, *args: Any, **kwargs: Any) -> str | Any:
        response_model, messages = self._pop_structured_request(args, kwargs)
        if self._state.fallback_active:
            if response_model is not None:
                return _structured_completion(self._fallback_llm, response_model, messages)
            return self._fallback_llm.call(*args, **kwargs)
        try:
            if self._primary_is_forced_down():
                raise ConnectionError("Primary model disabled by chaos configuration")
            if response_model is not None:
                return _structured_completion(self, response_model, messages)
            return super().call(*args, **kwargs)
        except Exception as error:
            self._activate_fallback(error)
            if response_model is not None:
                return _structured_completion(self._fallback_llm, response_model, messages)
            return self._fallback_llm.call(*args, **kwargs)

    async def acall(self, *args: Any, **kwargs: Any) -> str | Any:
        response_model, messages = self._pop_structured_request(args, kwargs)
        if self._state.fallback_active:
            if response_model is not None:
                return await _astructured_completion(self._fallback_llm, response_model, messages)
            return await self._fallback_llm.acall(*args, **kwargs)
        try:
            if self._primary_is_forced_down():
                raise ConnectionError("Primary model disabled by chaos configuration")
            if response_model is not None:
                return await _astructured_completion(self, response_model, messages)
            return await super().acall(*args, **kwargs)
        except Exception as error:
            self._activate_fallback(error)
            if response_model is not None:
                return await _astructured_completion(self._fallback_llm, response_model, messages)
            return await self._fallback_llm.acall(*args, **kwargs)


def build_llm(temperature: float = 0.2) -> GatewayLLM:
    """Build a CrewAI model routed only through the Makers model gateway."""
    state = _run_state.get()
    if state is None:
        state = _RunState(incident_id="unknown")
        _run_state.set(state)
    base_url = _required_setting("LLM_BASE_URL")
    api_key = _required_setting("MAKERS_MODELS_KEY")
    primary_model = _required_setting("LLM_PRIMARY_MODEL")
    fallback_model = _required_setting("LLM_FALLBACK_MODEL")
    # crewai 1.x structured outputs (Task.output_pydantic) route through
    # InternalInstructor, which invokes litellm's bare completion() with only the
    # model name — instance api_key/base_url never reach it, so litellm reads
    # these env vars instead. Publishing the gateway credentials here keeps that
    # leaf call on the Makers gateway without a second config path.
    os.environ.setdefault("OPENAI_API_KEY", api_key)
    os.environ.setdefault("OPENAI_API_BASE", base_url)
    return GatewayLLM(
        model=_litellm_model(primary_model),
        base_url=base_url,
        api_key=api_key,
        primary_model=primary_model,
        fallback_model=fallback_model,
        temperature=temperature,
        state=state,
    )
