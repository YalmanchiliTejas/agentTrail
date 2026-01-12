from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Callable, Optional

from .context import get_current_session


@dataclass(frozen=True)
class LLMUsage:
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    input_cost: float
    output_cost: float
    total_cost: float


def _calculate_cost(tokens: int, rate_per_1k: float) -> float:
    return round((tokens / 1000.0) * rate_per_1k, 6)


def _request_fingerprint(payload: Optional[dict[str, Any]]) -> Optional[str]:
    if payload is None:
        return None
    json_str = json.dumps(payload, sort_keys=True, default=repr)
    return hashlib.sha256(json_str.encode("utf-8")).hexdigest()


def _openai_usage_from_response(
    response: Any,
    provider: str,
    model: str,
    input_rate: float,
    output_rate: float,
) -> LLMUsage:
    usage = getattr(response, "usage", None) or (response.get("usage", {}) if isinstance(response, dict) else {})
    prompt_tokens = int(usage.get("prompt_tokens", 0))
    completion_tokens = int(usage.get("completion_tokens", 0))
    total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens))
    input_cost = _calculate_cost(prompt_tokens, input_rate)
    output_cost = _calculate_cost(completion_tokens, output_rate)
    return LLMUsage(
        provider=provider,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        input_cost=input_cost,
        output_cost=output_cost,
        total_cost=round(input_cost + output_cost, 6),
    )


def _anthropic_usage_from_response(
    response: Any,
    provider: str,
    model: str,
    input_rate: float,
    output_rate: float,
) -> LLMUsage:
    usage = getattr(response, "usage", None) or (response.get("usage", {}) if isinstance(response, dict) else {})
    prompt_tokens = int(usage.get("input_tokens", 0))
    completion_tokens = int(usage.get("output_tokens", 0))
    total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens))
    input_cost = _calculate_cost(prompt_tokens, input_rate)
    output_cost = _calculate_cost(completion_tokens, output_rate)
    return LLMUsage(
        provider=provider,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        input_cost=input_cost,
        output_cost=output_cost,
        total_cost=round(input_cost + output_cost, 6),
    )


def _ollama_usage_from_response(
    response: Any,
    provider: str,
    model: str,
    input_rate: float,
    output_rate: float,
) -> LLMUsage:
    # Ollama responses often contain token counts as eval counters.
    prompt_tokens = int(response.get("prompt_eval_count", 0)) if isinstance(response, dict) else 0
    completion_tokens = int(response.get("eval_count", 0)) if isinstance(response, dict) else 0
    total_tokens = int(prompt_tokens + completion_tokens)
    input_cost = _calculate_cost(prompt_tokens, input_rate)
    output_cost = _calculate_cost(completion_tokens, output_rate)
    return LLMUsage(
        provider=provider,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        input_cost=input_cost,
        output_cost=output_cost,
        total_cost=round(input_cost + output_cost, 6),
    )


def wrap_openai_call(
    *,
    model: str,
    call: Callable[[], Any],
    input_cost_per_1k: float,
    output_cost_per_1k: float,
    provider: str = "openai",
    tool_name: Optional[str] = None,
    request_payload: Optional[dict[str, Any]] = None,
) -> Any:
    session = get_current_session()
    if session is None:
        return call()
    request_fingerprint = _request_fingerprint(request_payload)
    return session.execute_llm_call(
        provider=provider,
        model=model,
        tool_name=tool_name or f"llm.{provider}",
        call=call,
        usage_parser=lambda response: _openai_usage_from_response(
            response, provider, model, input_cost_per_1k, output_cost_per_1k
        ),
        request_fingerprint=request_fingerprint,
    )


def wrap_anthropic_call(
    *,
    model: str,
    call: Callable[[], Any],
    input_cost_per_1k: float,
    output_cost_per_1k: float,
    provider: str = "anthropic",
    tool_name: Optional[str] = None,
    request_payload: Optional[dict[str, Any]] = None,
) -> Any:
    session = get_current_session()
    if session is None:
        return call()
    request_fingerprint = _request_fingerprint(request_payload)
    return session.execute_llm_call(
        provider=provider,
        model=model,
        tool_name=tool_name or f"llm.{provider}",
        call=call,
        usage_parser=lambda response: _anthropic_usage_from_response(
            response, provider, model, input_cost_per_1k, output_cost_per_1k
        ),
        request_fingerprint=request_fingerprint,
    )


def wrap_ollama_call(
    *,
    model: str,
    call: Callable[[], Any],
    input_cost_per_1k: float,
    output_cost_per_1k: float,
    provider: str = "ollama",
    tool_name: Optional[str] = None,
    request_payload: Optional[dict[str, Any]] = None,
) -> Any:
    session = get_current_session()
    if session is None:
        return call()
    request_fingerprint = _request_fingerprint(request_payload)
    return session.execute_llm_call(
        provider=provider,
        model=model,
        tool_name=tool_name or f"llm.{provider}",
        call=call,
        usage_parser=lambda response: _ollama_usage_from_response(
            response, provider, model, input_cost_per_1k, output_cost_per_1k
        ),
        request_fingerprint=request_fingerprint,
    )
