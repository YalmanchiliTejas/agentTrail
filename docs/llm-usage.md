# LLM usage tracking

AgentRelay includes helper wrappers that record token usage and cost for LLM calls. These wrappers create internal tool calls with provider/model metadata, and they contribute to the session-wide budget.

## OpenAI wrapper

```python
from agent_relay.llm import wrap_openai_call

response = wrap_openai_call(
    model="gpt-4o-mini",
    input_cost_per_1k=0.15,
    output_cost_per_1k=0.60,
    request_payload={"messages": [{"role": "user", "content": "Hello"}]},
    call=lambda: client.responses.create(model="gpt-4o-mini", input="Hello"),
)
```

## Anthropic wrapper

```python
from agent_relay.llm import wrap_anthropic_call

response = wrap_anthropic_call(
    model="claude-3-5-sonnet",
    input_cost_per_1k=3.0,
    output_cost_per_1k=15.0,
    request_payload={"messages": [{"role": "user", "content": "Hello"}]},
    call=lambda: client.messages.create(model="claude-3-5-sonnet", messages=[{"role": "user", "content": "Hello"}]),
)
```

## Ollama wrapper

```python
from agent_relay.llm import wrap_ollama_call

response = wrap_ollama_call(
    model="llama3.2",
    input_cost_per_1k=0.0,
    output_cost_per_1k=0.0,
    request_payload={"prompt": "Hello"},
    call=lambda: ollama_client.generate(model="llama3.2", prompt="Hello"),
)
```

## Budget interactions

When a wrapper captures token usage, AgentRelay updates the session totals. If the total cost exceeds `budget_limit`, a `BudgetExceededError` is raised and compensation will run unless you disable it with `compensate_on_budget_exceeded=False` on the session.

## Request fingerprints

`request_payload` is hashed and stored as `request_fingerprint` to help correlate repeated prompts or request shapes across runs.
