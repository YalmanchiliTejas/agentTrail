"""
examples/payment_demo.py

Small "checkout" workflow that intentionally fails at place_order() to show:
- forward execution (reserve_funds -> place_order -> send_receipt),
- failure and saga compensation (refund_funds),
- idempotency (re-run shows cache hits for the same input),
- run summary output at the end (ready for a visual dashboard).

To run:
    python -m examples.payment_demo

Change place_order to succeed to see a fully completed run.
"""

import os, json
from agenttrail import AgentTrail, step, compensate

# Where to keep the SQLite file. Default lives in /mnt/data for easy inspection.
DB_PATH = os.environ.get("AT_DB", "/mnt/data/agenttrail_demo/agent.db")

# Create a runtime instance for the "checkout" workflow. Tracing disabled for simplicity.
trail = AgentTrail(db_path=DB_PATH, workflow="checkout", enable_tracing=False)

@step()
def reserve_funds(user_id: str, amount_cents: int):
    """
    Step 1: Reserve funds at a payment service provider (PSP).
    In a real system we'd call out to PSP and persist the hold_id in tool_calls.
    Here we synthesize a deterministic id to keep the example pure and testable.
    """
    hold_id = f"HOLD-{user_id}-{amount_cents}"
    return {"hold_id": hold_id}

@compensate(for_=reserve_funds)
def refund_funds(original_step_id: str):
    """
    Compensation for reserve_funds().
    Given the original step's ID (and ideally its side-effect payload), attempt to undo it.
    In production, you would read the persisted hold_id and call PSP refund with its own idempotency key.
    """
    return {"refunded_step": original_step_id}

@step()
def place_order(user_id: str, sku: str, qty: int):
    """
    Step 2: Place the order in your OMS/inventory system.
    We raise an exception to simulate a business failure (e.g., no stock).
    This triggers saga compensation.
    """
    raise RuntimeError(f"Inventory unavailable for sku={sku}")

@step()
def send_receipt(user_id: str, email: str):
    """
    Step 3: Send a receipt. This won't run in the failing path,
    but you can make place_order succeed and observe it.
    """
    return {"sent_to": email}

def main():
    # Start a run, so all subsequent steps/events are correlated.
    run_id = trail.start_run()

    # Connect our compensator to the step it undoes.
    trail.register_compensator("reserve_funds", refund_funds)

    try:
        # Forward path. Each call goes through the runtime wrapper (idempotency, events, etc.).
        rf = trail.wrap(reserve_funds)("alice", 1299)
        po = trail.wrap(place_order)("alice", "SKU-42", 1)  # <- raises
        sr = trail.wrap(send_receipt)("alice", "alice@example.com")
        trail.end_run("completed")
    except Exception as e:
        # On any exception, unwind completed steps (in reverse) via saga compensation.
        trail.compensate()
        trail.end_run("failed")

    # Print a compact summary you could feed to a dashboard.
    summary = trail.get_run_summary(run_id)
    print(json.dumps(summary, indent=2))

if __name__ == "__main__":
    main()
