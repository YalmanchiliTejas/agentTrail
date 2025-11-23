# demo_hospital_agent.py VIBECODED

import os
import uuid
from typing import Any, Dict

from google import genai  # pip install google-genai

from agent_relay.runtime import AgentRuntime
from agent_relay.tooling import tool
import argparse

# -------------------------------------------------------------------
# 1) Runtime & DB setup
# -------------------------------------------------------------------

# Example: postgres://user:pass@localhost:5432/agent_runtime_demo


runtime = AgentRuntime.from_conection_string("db_connection_string")

# Gemini client – will read GEMINI_API_KEY / GOOGLE_API_KEY from env
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "your_gemini_api_key_here")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)


# -------------------------------------------------------------------
# 2) Tools (what your agent can do)
# -------------------------------------------------------------------

@tool(runtime, name="generate_invoice_email_body")
def generate_invoice_email_body(patient_name: str, amount_cents: int) -> str:
    """Use Gemini to draft a short invoice email body."""
    dollars = amount_cents / 100.0
    prompt = (
        f"Write a short, professional email to patient {patient_name} "
        f"explaining that their invoice for ${dollars:.2f} is ready. "
        "Keep it under 150 words."
    )

    response = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )
    return response.text


@tool(runtime, name="send_invoice_email", compensation="cancel_invoice_email")
def send_invoice_email(to_email: str, subject: str, body: str) -> str:
    """
    In a real system this would call SES/SendGrid/Gmail, etc.
    For demo we just log and return a fake message_id.
    """
    message_id = f"msg-{uuid.uuid4()}"
    print(f"[SEND] email -> {to_email} (subject={subject}) (message_id={message_id})")
    return message_id


@tool(runtime, name="cancel_invoice_email")
def cancel_invoice_email(to_email: str, subject: str, body: str) -> None:
    """
    Compensation for send_invoice_email.
    In a real system this might issue a 'recall' or send a follow-up correction.
    """
    print(f"[CANCEL] email to {to_email} (subject={subject}) – compensation invoked.")


# --- Extra tool for idempotency test --------------------------------

CALL_COUNTER = {"fetch_patient_record": 0}


@tool(runtime, name="fetch_patient_record")
def fetch_patient_record(patient_id: str) -> Dict[str, Any]:
    """
    Dummy 'expensive' tool to show idempotency.
    Underlying function should run only once for the same args.
    """
    CALL_COUNTER["fetch_patient_record"] += 1
    print(
        f"[TOOL] actually looking up patient {patient_id} "
        f"(call #{CALL_COUNTER['fetch_patient_record']})"
    )
    return {
        "patient_id": patient_id,
        "plan": "PPO-123",
        "balance_cents": 12345,
    }


# -------------------------------------------------------------------
# 3) Agent logic (business workflows)
# -------------------------------------------------------------------

def hospital_billing_agent(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Example hospital billing agent:

    1. Calls Gemini to draft the invoice email.
    2. Sends the email.
    3. Optionally throws if email was sent to wrong recipient to show compensation.
    """

    patient_name = event["patient_name"]
    correct_email = event["correct_email"]
    wrong_email = event.get("wrong_email")
    amount_cents = event["amount_cents"]
    # Step 1: draft email body with Gemini
    body = generate_invoice_email_body(patient_name=patient_name, amount_cents=amount_cents)
    subject = f"Invoice for {patient_name}"

    # Step 2: optionally send to the WRONG email to show saga compensation
    if wrong_email:
        print("\n--- sending to WRONG email to trigger compensation ---")
        msg_id_wrong = send_invoice_email(to_email=wrong_email, subject=subject, body=body)
        # Simulate detection of mistake -> raise
        raise RuntimeError(
            f"Invoice sent to wrong recipient: {wrong_email} (msg_id={msg_id_wrong})"
        )

    # Correct path: send to right patient email
    print("\n--- sending to CORRECT email ---")
    msg_id = send_invoice_email(to_email=correct_email, subject=subject, body=body)

    return {
        "patient_name": patient_name,
        "email": correct_email,
        "amount_cents": amount_cents,
        "message_id": msg_id,
    }


def idempotency_agent(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Agent that calls the same tool 3 times with identical args.
    The underlying function should run only once (idempotency).
    """
    patient_id = event["patient_id"]

    print("\n--- calling fetch_patient_record THREE times with same args ---")
    r1 = fetch_patient_record(patient_id)
    r2 = fetch_patient_record(patient_id)
    r3 = fetch_patient_record(patient_id)

    # should all be identical objects
    return {
        "r1": r1,
        "r2": r2,
        "r3": r3,
        "call_count": CALL_COUNTER["fetch_patient_record"],
    }


# -------------------------------------------------------------------
# 4) Test runners
# -------------------------------------------------------------------

def run_saga_demo() -> None:
    """
    Test 1: Saga / compensation.
    Wrong recipient -> send + compensation.
    """
    print("\n=== TEST 1: SAGA / COMPENSATION DEMO ===")
    initial_event = {
        "patient_name": "Alice Smith",
        "correct_email": "alice@example.com",
        "wrong_email": "bob@example.com",  # triggers error to show compensation
        "amount_cents": 12345,
    }

    session = None
    try:
        with runtime.agent_session(
            name="hospital_billing_saga",
            input_payload=initial_event,
        ) as session:
            result = hospital_billing_agent(initial_event)
            session.set_output(result)
    except RuntimeError as e:
        print(f"[AGENT ERROR] {e}")
        if session and session.run_id:
            print(f"[RUN ID] {session.run_id}")
        print(
            "→ Check tool_calls: you should see send_invoice_email (forward) "
            "and cancel_invoice_email (compensation)."
        )


def run_replay_demo() -> None:
    """
    Test 2: Deterministic replay.
    1) Forward run with correct email (records tool_calls).
    2) Replay run with replay=True, using recorded calls (no real Gemini / email).
    """
    print("\n=== TEST 2: DETERMINISTIC REPLAY DEMO ===")

    fixed_event = {
        "patient_name": "Alice Smith",
        "correct_email": "alice@example.com",
        "wrong_email": None,  # no bug this time
        "amount_cents": 98765,
    }

    # ---- FORWARD RUN (records tool_calls) ----
    with runtime.agent_session(
        name="hospital_billing_replay_demo",
        input_payload=fixed_event,
    ) as session:
        forward_result = hospital_billing_agent(fixed_event)
        session.set_output(forward_result)
        forward_run_id = session.run_id

    print(f"[FORWARD RUN ID] {forward_run_id}")
    print("[FORWARD OUTPUT]", forward_result)

    # ---- REPLAY RUN (no real tools) ----
    with runtime.agent_session(
        name="hospital_billing_replay_demo",
        input_payload={"replay_of": forward_run_id},
        replay=True,
        replay_run_id=forward_run_id,
    ) as replay_session:
        print("\n--- calling agent in REPLAY mode (no real Gemini / email) ---")
        replay_result = hospital_billing_agent(fixed_event)
        replay_session.set_output(replay_result)

    print("[REPLAY OUTPUT]", replay_result)

    # Normalize IDs in case of extra quoting from JSON serialization
    f_id = str(forward_result["message_id"]).strip('"')
    r_id = str(replay_result["message_id"]).strip('"')
    print(f"[CHECK] message_id equal after replay? {f_id == r_id}")
    print(
        "→ During REPLAY you should NOT see any [SEND] lines, "
        "because send_invoice_email is served from tool_calls."
    )


def run_idempotency_demo() -> None:
    """
    Test 3: Idempotency.
    Tool is called 3 times with same args, but underlying function runs once.
    """
    print("\n=== TEST 3: IDEMPOTENCY DEMO ===")
    CALL_COUNTER["fetch_patient_record"] = 0

    event = {"patient_id": "P-12345"}

    with runtime.agent_session(
        name="idempotency_demo",
        input_payload=event,
    ) as session:
        result = idempotency_agent(event)
        session.set_output(result)

    print("[IDEMPOTENCY RESULT]", result)
    print(
        f"[CHECK] underlying fetch_patient_record called "
        f"{CALL_COUNTER['fetch_patient_record']} time(s) "
        "(should be 1 if idempotency works)."
    )


# -------------------------------------------------------------------
# 5) CLI entrypoint
# -------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Agent runtime demo for hospital billing.")
    parser.add_argument(
        "--test",
        choices=["saga", "replay", "idempotency", "all"],
        default="all",
        help="Which demo to run.",
    )
    args = parser.parse_args()

    if args.test in ("saga", "all"):
        run_saga_demo()

    if args.test in ("replay", "all"):
        run_replay_demo()

    if args.test in ("idempotency", "all"):
        run_idempotency_demo()


if __name__ == "__main__":
    main()