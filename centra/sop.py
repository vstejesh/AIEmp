"""SOP = the job description an AI employee follows. It is data, not code:
the same runtime executes any SOP that validates against this schema."""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field as PField, model_validator

SOP_DIR = Path(__file__).resolve().parent.parent / "sops"

# Every action an SOP may reference, with what it means. Actions are the only
# way an AI employee changes the world; the policy gate and verifier key off them.
ACTIONS = {
    "ats.set_status=interview": "Set the applicant's ATS status to Interview (browser).",
    "ats.set_status=rejected": "Set the applicant's ATS status to Rejected (browser).",
    "calendar.book": "Book one interview slot with the subject (calendar API).",
    "mail.send=<template>": "Send one email using the named template (mail API).",
    "helpdesk.reply": "Post a reply on the ticket, leaving it open (browser).",
    "helpdesk.reply+close": "Post a reply and close the ticket (browser).",
    "helpdesk.tag=<tag>": "Add a tag to the ticket: finance, engineering or triage (browser).",
}
SCOPE_SOURCES = {
    "ats": "Applicants in the ATS. Filter keys: role, status.",
    "helpdesk": "Tickets in the helpdesk. Filter keys: status.",
}


class Field(BaseModel):
    name: str
    type: Literal["number", "bool", "string", "enum"]
    description: str
    options: list[str] = []
    source: str = PField(description="Where the value is found, e.g. 'resume file' or 'ATS applicant page'.")


class Rule(BaseModel):
    when: str = PField(description="Expression over field names, e.g. 'referral == true', or 'else'.")
    outcome: str


class Template(BaseModel):
    to: Literal["subject", "owner"] = "subject"
    subject: str
    body: str = PField(description="May use {name} and {slot} placeholders.")


class Constraint(BaseModel):
    days: list[Literal["Mon", "Tue", "Wed", "Thu", "Fri"]] = []
    hours: list[int] = []


class SOP(BaseModel):
    id: str
    name: str
    owner: str
    version: int = 1
    scope: dict = PField(description="{'source': 'ats'|'helpdesk', 'filter': {...}}")
    steps: list[str]
    fields: list[Field]
    rules: list[Rule]
    outcomes: dict[str, list[str]]
    approval_required: list[str] = []
    constraints: dict[str, Constraint] = {}
    templates: dict[str, Template] = {}
    notes: list[str] = PField(default=[], description="Clarifications learned from the owner.")
    defaults: dict[str, str] = {}

    @model_validator(mode="after")
    def _check(self):
        problems = validate_semantics(self)
        if problems:
            raise ValueError("; ".join(problems))
        return self


def _action_known(a: str, templates: dict) -> bool:
    if a in ACTIONS:
        return True
    if a.startswith("mail.send="):
        return a.split("=", 1)[1] in templates
    if a.startswith("helpdesk.tag="):
        return a.split("=", 1)[1] in {"finance", "engineering", "triage"}
    return False


def validate_semantics(s: SOP) -> list[str]:
    p = []
    if s.scope.get("source") not in SCOPE_SOURCES:
        p.append(f"scope.source must be one of {list(SCOPE_SOURCES)}")
    names = {f.name for f in s.fields}
    if not s.rules or s.rules[-1].when.strip() != "else":
        p.append("the last rule must be 'else'")
    for r in s.rules:
        if r.outcome not in s.outcomes:
            p.append(f"rule outcome '{r.outcome}' is not in outcomes")
        if r.when.strip() != "else":
            try:
                missing = _names(r.when) - names
                if missing:
                    p.append(f"rule '{r.when}' uses unknown fields {sorted(missing)}")
            except ValueError as e:
                p.append(str(e))
    for o, acts in s.outcomes.items():
        for a in acts:
            if not _action_known(a, s.templates):
                p.append(f"outcome '{o}' uses unknown action '{a}'")
    for a in s.approval_required + list(s.constraints):
        if not any(x == a or x.startswith(a) for acts in s.outcomes.values() for x in acts):
            p.append(f"'{a}' in approvals/constraints is never used by an outcome")
    return p


# ---------------------------------------------------------------- rule evaluation
_ALLOWED = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.Compare, ast.Name,
            ast.Load, ast.Constant, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
            ast.List, ast.Tuple)


def _parse(expr: str) -> ast.Expression:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        raise ValueError(f"rule '{expr}' is not a valid expression")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ValueError(f"rule '{expr}' uses a disallowed construct ({type(node).__name__})")
    return tree


def _names(expr: str) -> set[str]:
    return {n.id for n in ast.walk(_parse(expr)) if isinstance(n, ast.Name)} - {"true", "false"}


def evaluate(sop: SOP, facts: dict) -> tuple[str | None, str]:
    """First matching rule wins. Returns (outcome, explanation); outcome None if
    a field a rule needs is missing, so the agent must go find it or ask."""
    for r in sop.rules:
        if r.when.strip() == "else":
            return r.outcome, "no earlier rule matched (else)"
        needed = _names(r.when)
        missing = [n for n in needed if n not in facts]
        if missing:
            return None, f"cannot evaluate rule '{r.when}': missing facts {missing}"
        env = {"true": True, "false": False, **{n: facts[n] for n in needed}}
        if eval(compile(_parse(r.when), "<rule>", "eval"), {"__builtins__": {}}, env):
            return r.outcome, f"rule '{r.when}' matched"
    return None, "no rule matched"


def coerce(field: Field, value):
    if field.type == "number":
        return float(value)
    if field.type == "bool":
        if isinstance(value, bool):
            return value
        if str(value).strip().lower() in {"true", "yes", "1"}:
            return True
        if str(value).strip().lower() in {"false", "no", "0"}:
            return False
        raise ValueError(f"'{value}' is not a boolean")
    if field.type == "enum":
        if value not in field.options:
            raise ValueError(f"'{value}' is not one of {field.options}")
    return str(value)


# ---------------------------------------------------------------- storage (versioned)
def save(sop: SOP) -> Path:
    SOP_DIR.mkdir(exist_ok=True)
    p = SOP_DIR / f"{sop.id}.v{sop.version}.json"
    p.write_text(sop.model_dump_json(indent=2))
    return p


def load(sop_id: str, version: int | None = None) -> SOP:
    files = sorted(SOP_DIR.glob(f"{sop_id}.v*.json"), key=lambda f: int(f.stem.rsplit(".v", 1)[1]))
    if not files:
        raise FileNotFoundError(sop_id)
    f = files[-1] if version is None else SOP_DIR / f"{sop_id}.v{version}.json"
    return SOP.model_validate(json.loads(f.read_text()))


def list_all() -> list[SOP]:
    ids = sorted({f.stem.rsplit(".v", 1)[0] for f in SOP_DIR.glob("*.v*.json")})
    return [load(i) for i in ids]
