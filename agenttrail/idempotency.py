"""
agenttrail/idempotency.py

Idempotency is critical for "exactly-once" semantics at the step boundary.
We generate a stable, collision-resistant key from:
  - namespace (workflow name),
  - step_name,
  - *args and **kwargs (canonically serialized with type tags).

Why type tags?
- "1" (str) and 1 (int) must not collide.
- dict keys are sorted to avoid non-determinism from insertion order.
"""

import json
import hashlib
from typing import Any

def _tagged(o: Any):
    """
    Recursively convert Python objects into a JSON structure that carries type information.
    This avoids ambiguous collisions (e.g., 1 vs "1") and ensures stable ordering for dicts.
    """
    if o is None or isinstance(o, (bool, int, float, str)):
        return {"__type__": type(o).__name__, "value": o}
    if isinstance(o, (list, tuple)):
        return {"__type__": "list", "value": [_tagged(v) for v in o]}
    if isinstance(o, dict):
        # Sort keys for canonical order; value side is recursively tagged.
        return {"__type__": "dict", "value": {k: _tagged(o[k]) for k in sorted(o)}}
    # Fallback: store string representation and explicit type to remain stable.
    return {"__type__": type(o).__name__, "value": str(o)}

def canonical_json(obj: Any) -> str:
    """
    Deterministically serialize to JSON:
    - remove pretty whitespace via compact separators,
    - sort keys,
    - use our tagged representation above.
    """
    return json.dumps(_tagged(obj), separators=(",", ":"), sort_keys=True, ensure_ascii=False)

def idem_key(namespace: str, step_name: str, args: tuple, kwargs: dict) -> str:
    """
    Compute the idempotency key for a step call.
    Include namespace and step_name so two different workflows can't collide.
    """
    payload = {"ns": namespace, "step": step_name, "args": args, "kwargs": kwargs}
    s = canonical_json(payload)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()
