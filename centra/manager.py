"""Centra: the layer above the AI employees.
  learn_sop   - a department head's plain-text description -> a validated SOP draft for review
  review_run  - after a run, turn what went wrong or was unclear into a proposed SOP revision
Neither writes anything until a human approves it."""
from __future__ import annotations

import difflib
import json

from pydantic import ValidationError

from . import sop as sop_mod
from .llm import LLM
from .sop import ACTIONS, SCOPE_SOURCES, SOP
from .state import Run

SCHEMA_DOC = """SOP JSON schema:
{
 "id": "kebab-case id", "name": "job title", "owner": "<owner email>", "version": 1,
 "scope": {"source": "ats" | "helpdesk", "filter": {...}},        // which subjects the job covers
 "steps": ["plain-language steps, in order, including where things are in the apps"],
 "fields": [{"name": "snake_case", "type": "number|bool|string|enum", "options": [...enum only],
             "description": "...", "source": "where the value is found"}],
 "rules": [{"when": "<python-style expression over field names, true/false lowercase>", "outcome": "<outcome>"},
           ..., {"when": "else", "outcome": "<outcome>"}],          // ordered, first match wins, last is else
 "outcomes": {"<outcome>": ["<action>", ...]},                     // actions from the catalogue only
 "approval_required": ["<action or prefix>"],                      // actions the owner must approve first
 "constraints": {"calendar.book": {"days": ["Tue"], "hours": [14, 15]}},
 "templates": {"<name>": {"to": "subject" | "owner", "subject": "...", "body": "... {name} {slot} {note} {subject_id}"}},
 "notes": ["clarifications and exceptions the owner stated"]
}"""

LEARN_SYSTEM = f"""You are Centra, an AI automation head. A department head has described a repeated process.
Turn it into a precise SOP an AI employee can execute. Capture every rule and exception they stated, and the
order rules must apply in (exceptions before general rules). Do not invent rules they did not state; record
anything unclear in notes as an open question. Use only these actions:
{json.dumps(ACTIONS, indent=1)}
Scope sources: {json.dumps(SCOPE_SOURCES)}
{SCHEMA_DOC}
Reply with ONE JSON object: {{"sop": <the SOP>, "open_questions": ["..."]}}"""

REVIEW_SYSTEM = f"""You are Centra, managing an AI employee. You get the SOP it ran under and what happened in
the run: issues the employee reported, questions the owner answered, failures, and verifier results.
Propose the smallest SOP revision that would stop the same problem recurring:
- an owner's answer to a recurring question becomes a note (or a rule, if it is clearly general),
- an app that differs from the SOP's description becomes a corrected step,
- do not change rules that worked. If nothing should change, return the SOP unchanged with no changes.
{SCHEMA_DOC}
Reply with ONE JSON object: {{"changes": [{{"what": "...", "why": "evidence from this run"}}], "sop": <full revised SOP>}}"""


def _validated(llm: LLM, system: str, user: str, tries: int = 3) -> tuple[dict, SOP]:
    msg, err = user, None
    for _ in range(tries):
        out = llm.json(system, msg if not err else f"{msg}\n\nYour last SOP failed validation: {err}\nFix it.")
        try:
            return out, SOP.model_validate(out.get("sop", {}))
        except ValidationError as e:
            err = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())[:800]
    raise ValueError(f"could not produce a valid SOP: {err}")


def learn_sop(description: str, owner: str, sop_id: str, llm: LLM | None = None) -> dict:
    llm = llm or LLM()
    out, s = _validated(llm, LEARN_SYSTEM,
                        f"Owner: {owner}\nUse id: {sop_id}\n\nTheir description, verbatim:\n\"\"\"{description}\"\"\"")
    s = s.model_copy(update={"id": sop_id, "owner": owner, "version": 1})
    return {"sop": s.model_dump(), "open_questions": out.get("open_questions", []),
            "usage": llm.usage.as_dict(llm.provider)}


def review_run(run: Run, llm: LLM | None = None) -> dict:
    current = sop_mod.load(run.sop_id, run.sop_version)
    signals = {
        "employee_reported_sop_issues": run.sop_issues,
        "owner_answers": run.human_answers,
        "failed_steps": [{"tool": e["tool"], "error": e["result"][:300]} for e in run.log
                         if e.get("kind") == "step" and not e["ok"]][-12:],
        "verifier_failures": (run.verification or {}).get("failed", []),
        "status": run.status,
    }
    if not (run.sop_issues or run.human_answers or signals["verifier_failures"] or run.status != "completed"):
        return {"changes": [], "diff": "", "note": "clean run; nothing to change"}
    llm = llm or LLM()
    out, revised = _validated(llm, REVIEW_SYSTEM,
                              f"CURRENT SOP:\n{current.model_dump_json(indent=1)}\n\nRUN SIGNALS:\n{json.dumps(signals, indent=1)}")
    revised = revised.model_copy(update={"id": current.id, "owner": current.owner, "version": current.version + 1})
    diff = "\n".join(difflib.unified_diff(current.model_dump_json(indent=1).splitlines(),
                                          revised.model_dump_json(indent=1).splitlines(),
                                          f"v{current.version}", f"v{revised.version}", lineterm="", n=1))
    return {"changes": out.get("changes", []), "diff": diff, "sop": revised.model_dump(),
            "usage": llm.usage.as_dict(llm.provider)}


def approve_sop(data: dict) -> SOP:
    s = SOP.model_validate(data)
    try:
        latest = sop_mod.load(s.id)
        s = s.model_copy(update={"version": latest.version + 1})
    except FileNotFoundError:
        s = s.model_copy(update={"version": 1})
    sop_mod.save(s)
    return s
