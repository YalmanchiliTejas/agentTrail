# demo_llm_job_apply_saga.py
# A full demo that shows: real LLM tool usage, idempotency, and Saga compensation.
# Uses your AgentTrail runtime with DX additions (autowrap, run, auto-registered compensators).

import os, json, time
from dataclasses import dataclass, asdict
from typing import Dict, Any, Optional

from agenttrail.runtime import AgentTrail, step, compensate

# ================
# Fake external backends (side effects we must compensate)
# ================
class FakePayments:
    def __init__(self):
        self.holds: Dict[str, Dict[str, Any]] = {}  # hold_id -> data
    def reserve(self, hold_id: str, cents: int):
        if hold_id not in self.holds:
            self.holds[hold_id] = {"cents": cents, "active": True}
        return {"hold_id": hold_id}
    def refund(self, hold_id: str):
        if hold_id in self.holds:
            self.holds[hold_id]["active"] = False
        return {"refunded": True, "hold_id": hold_id}

class FakeATS:
    def __init__(self):
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.apps: Dict[str, Dict[str, Any]] = {}
    def seed_job(self, job_id: str, title: str, fields: Dict[str, str]):
        self.jobs[job_id] = {"job_id": job_id, "title": title, "fields": fields}
    def get_job(self, job_id: str) -> Dict[str, Any]:
        if job_id not in self.jobs:
            raise ValueError(f"Job not found: {job_id}")
        return self.jobs[job_id]
    def create_or_get_draft(self, app_id: str, payload: Dict[str, Any]):
        if app_id not in self.apps:
            self.apps[app_id] = {"status": "draft", "app_id": app_id, **payload}
        return self.apps[app_id]
    def update_app(self, app_id: str, updates: Dict[str, Any]):
        self.apps[app_id].update(updates); return self.apps[app_id]
    def delete_draft(self, app_id: str):
        if app_id in self.apps and self.apps[app_id].get("status") == "draft":
            del self.apps[app_id]
            return {"deleted": True, "app_id": app_id}
        return {"deleted": False, "app_id": app_id}
    def submit(self, app_id: str):
        self.apps[app_id]["status"] = "submitted"
        self.apps[app_id]["submitted_at"] = time.time()
        return {"ok": True, "app_id": app_id}
    def withdraw(self, app_id: str):
        if app_id in self.apps and self.apps[app_id]["status"] == "submitted":
            self.apps[app_id]["status"] = "withdrawn"
            return {"withdrawn": True, "app_id": app_id}
        return {"withdrawn": False, "app_id": app_id}

PAY = FakePayments()
ATS = FakeATS()
ATS.seed_job(
    "JOB-42",
    "Software Engineer (Systems)",
    fields={
        "full_name": "text",
        "email": "email",
        "phone": "text",
        "github": "url",
        "resume": "text",
        "cover_letter": "text",
    },
)

# ============================
# Candidate data
# ============================
@dataclass
class CandidateProfile:
    full_name: str
    email: str
    phone: str
    github: str
    resume_text: str
    cover_letter: str

# ============================
# LLM Provider abstraction
# ============================
class LLMProvider:
    def extract_fields(self, job_desc: str, profile: CandidateProfile) -> Dict[str, Any]:
        raise NotImplementedError

class OpenAIProvider(LLMProvider):
    def __init__(self, model: Optional[str] = None):
        from openai import OpenAI
        self.client = OpenAI()
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    def extract_fields(self, job_desc: str, profile: CandidateProfile) -> Dict[str, Any]:
        system = (
            "You output only a single JSON object. No extra text."
        )
        user = f"""
Job Description:
{job_desc}

Candidate Profile (JSON):
{json.dumps(asdict(profile), ensure_ascii=False)}

Return JSON with keys:
full_name, email, phone, github, resume, cover_letter.
resume must equal profile.resume_text; cover_letter must equal profile.cover_letter.
"""
        resp = self.client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[{"role":"system","content":system},{"role":"user","content":user}],
            temperature=0.1,
        )
        return json.loads(resp.choices[0].message.content)

class GeminiProvider(LLMProvider):
    def __init__(self, model: Optional[str] = None):
        import google.generativeai as genai
        genai.configure(api_key=os.environ["GEMINI_API_KEY"])
        self.genai = genai
        self.model_name = model or os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
        self.model = genai.GenerativeModel(self.model_name)
    def extract_fields(self, job_desc: str, profile: CandidateProfile) -> Dict[str, Any]:
        prompt = f"""
Output only JSON, no prose.
Given this job description and profile, return:
full_name, email, phone, github, resume, cover_letter.
resume must equal profile.resume_text; cover_letter must equal profile.cover_letter.

Job Description:
{job_desc}

Profile JSON:
{json.dumps(asdict(profile), ensure_ascii=False)}
"""
        resp = self.model.generate_content(
            prompt,
            generation_config={"response_mime_type": "application/json"}
        )
        return json.loads(resp.text)

def make_provider() -> LLMProvider:
    provider = (os.environ.get("PROVIDER") or "openai").lower()
    return GeminiProvider() if provider == "gemini" else OpenAIProvider()

# ======================
# Tools (steps) + compensators (SAGA)
# ======================

@step("load_job")
def load_job(job_id: str) -> Dict[str, Any]:
    return ATS.get_job(job_id)

@step("llm_extract_fields")
def llm_extract_fields(job: Dict[str, Any], profile: CandidateProfile) -> Dict[str, Any]:
    provider = make_provider()
    fields = provider.extract_fields(
        job_desc=f"{job['title']} — required fields {list(job['fields'].keys())}",
        profile=profile
    )
    # Validate presence
    required = set(job["fields"].keys())
    missing = [k for k in required if k not in fields or not str(fields[k]).strip()]
    if missing:
        raise ValueError(f"LLM missing fields: {missing}")
    return {"job_id": job["job_id"], "fields": fields}

@step("reserve_funds")
def reserve_funds(email: str, cents: int, idem: Optional[str] = None) -> Dict[str, Any]:
    """
    Idempotent reserve: hold_id is deterministic from email+cents.
    Passing idem makes the step-level idempotency key explicit too.
    """
    hold_id = f"HOLD:{email}:{cents}"
    return PAY.reserve(hold_id, cents)

@compensate(for_=reserve_funds)
def refund_funds(original_step_id: str):
    # In a prod build you'd load step result by step_id; here recompute hold_id from inputs or store elsewhere.
    # For demo we'll just mark a generic refund (no-op if already refunded).
    # (AgentTrail passes original_step_id so you can look up effect payload when persisted.)
    # We'll parse hold id back from events in a fuller build; here we just show the comp step firing.
    return {"compensation": "refund_attempted", "original_step_id": original_step_id}

@step("create_or_get_draft")
def create_or_get_draft(job_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    app_id = f"{job_id}:{fields['email']}"  # deterministic -> idempotent
    draft = ATS.create_or_get_draft(app_id, {"prepped": {"job_id": job_id, "fields": fields}})
    return {"app_id": app_id, "draft": draft}

@compensate(for_=create_or_get_draft)
def delete_draft(original_step_id: str):
    # With result persistence you'd fetch app_id; for demo, signal that we'd delete.
    return {"compensation": "delete_draft_attempted", "original_step_id": original_step_id}

@step("fill_form_fields")
def fill_form_fields(app_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    ATS.update_app(app_id, {"fields": fields, "fields_filled": True})
    return {"app_id": app_id, "fields_filled": True}

@compensate(for_=fill_form_fields)
def clear_filled_fields(original_step_id: str):
    return {"compensation": "clear_fields_attempted", "original_step_id": original_step_id}

@step("upload_resume")
def upload_resume(app_id: str, resume_text: str) -> Dict[str, Any]:
    ATS.update_app(app_id, {"resume_uploaded": True, "resume_len": len(resume_text)})
    return {"app_id": app_id, "resume_uploaded": True}

@compensate(for_=upload_resume)
def remove_resume_upload(original_step_id: str):
    return {"compensation": "remove_resume_attempted", "original_step_id": original_step_id}

@step("risk_check")
def risk_check(app_id: str, trigger_fail: bool = False) -> Dict[str, Any]:
    if trigger_fail:
        # Simulate a downstream failure AFTER several steps succeeded
        raise RuntimeError("External risk engine outage")
    return {"ok": True, "app_id": app_id}

@step("submit_application")
def submit_application(app_id: str) -> Dict[str, Any]:
    return ATS.submit(app_id)

@compensate(for_=submit_application)
def withdraw_application(original_step_id: str):
    # If failure happened AFTER submit, this would withdraw.
    return {"compensation": "withdraw_attempted", "original_step_id": original_step_id}

# ======================
# Orchestrating workflow
# ======================
def apply_with_compensation(job_id: str, profile: CandidateProfile, *, trigger_fail_midway: bool):
    job = load_job(job_id)
    prepped = llm_extract_fields(job, profile)

    # Step-level idempotency via explicit 'idem' AND business id via deterministic app_id
    reserve_funds(prepped["fields"]["email"], 2500, idem=f"{job_id}:{profile.email}:reserve")

    draft = create_or_get_draft(prepped["job_id"], prepped["fields"])
    fill_form_fields(draft["app_id"], prepped["fields"])
    upload_resume(draft["app_id"], profile.resume_text)

    # Induce failure here to trigger SAGA compensation of prior steps
    risk_check(draft["app_id"], trigger_fail=trigger_fail_midway)

    # If we got here, all good; final irreversible step
    return submit_application(draft["app_id"])

# ==============
# Demo main
# ==============
if __name__ == "__main__":
    profile = CandidateProfile(
        full_name="Jordan Patel",
        email="jordan.patel@example.com",
        phone="+1-317-555-0117",
        github="https://github.com/jordanpatel",
        resume_text=("Systems engineer focusing on Linux internals, concurrency, io_uring, "
                     "eBPF tracing, and perf tuning for low-latency services."),
        cover_letter=("Your systems role matches my expertise in kernel-level profiling and "
                      "coordination of high-throughput pipelines."),
    )

    trail = AgentTrail(db_path="agenttrail.sqlite", workflow="job-apply-saga")
    # Wrap steps in place so call sites remain the same
    import __main__ as ns
    trail.autowrap_namespace(ns.__dict__)

    print("\n=== RUN 1: induce failure to show SAGA compensation ===")
    try:
        trail.run(apply_with_compensation, "JOB-42", profile, trigger_fail_midway=True)
    except Exception as e:
        print("Expected failure:", e)

    from pprint import pprint
    print("\nRun 1 summary (should include STEP_FAILED and STEP_COMPENSATED events):")
    pprint(trail.get_run_summary())

    print("\nATS state after compensation (draft should be cleaned logically by compensators):")
    pprint(ATS.apps)
    pprint(PAY.holds)

    print("\n=== RUN 2: rerun with same inputs (idempotency cache hits) and no failure ===")
    # New trail for a clean run timeline (or reuse same, both fine)
    trail2 = AgentTrail(db_path="agenttrail.sqlite", workflow="job-apply-saga")
    trail2.autowrap_namespace(ns.__dict__)
    out = trail2.run(apply_with_compensation, "JOB-42", profile, trigger_fail_midway=False)
    print("Successful submit:", out)

    print("\nRun 2 summary (expect STEP_CACHE_HITs for repeated idempotent steps):")
    pprint(trail2.get_run_summary())

    print("\nFinal ATS state (submitted application present):")
    pprint(ATS.apps)
