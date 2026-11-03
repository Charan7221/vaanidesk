"""Send a patient to the right department and doctor from the problem they describe.

Callers (especially first-time patients) rarely know which doctor to see. The agent rewrites the caller's
problem in simple English and this module matches it against each department's ``treats`` list, then applies
age and gender rules (children to paediatrics, pregnancy to gynaecology). It never diagnoses: it only says
which OPD the patient should visit, and flags possible emergencies so the agent can send them to 108.
"""
from __future__ import annotations

import re

from ..business import Business, Department


def _norm(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip() + " "


def _has(text: str, phrase: str) -> bool:
    return _norm(phrase) in text


def eligible(dept: Department, age: int | None, gender: str | None) -> bool:
    if age is not None and dept.age_min is not None and age < dept.age_min:
        return False
    if age is not None and dept.age_max is not None and age > dept.age_max:
        return False
    if gender and dept.gender and gender.lower() != dept.gender.lower():
        return False
    return True


def recommend(biz: Business, problem: str, age: int | None = None, gender: str | None = None) -> dict:
    text = _norm(problem)
    red_flags = [w for w in biz.red_flags if _has(text, w)]
    scored = []
    for d in biz.departments:
        matched = [t for t in d.treats if _has(text, t)]
        if matched:
            # longer, more specific phrases win ("child fever" beats "fever"), and a department for a specific
            # group (children, women) wins a tie: "my son has a fever" -> paediatrics
            specific = 0.5 if (d.age_max is not None or d.gender) else 0
            scored.append((sum(len(t.split()) for t in matched) + specific, d, matched))
    scored.sort(key=lambda x: -x[0])
    ok = [(s, d, m) for s, d, m in scored if eligible(d, age, gender)]

    if ok:
        dept, matched, how = ok[0][1], ok[0][2], "matched the problem"
    else:
        # nothing fits (or the best match doesn't take this age/gender): children's department for a child,
        # otherwise the default (general) department, which can refer onwards.
        kids = [d for d in biz.departments if d.age_max is not None and eligible(d, age, gender)]
        default = [d for d in biz.departments if d.default and eligible(d, age, gender)]
        anyone = [d for d in biz.departments if eligible(d, age, gender)]
        pick = (kids if age is not None and kids else []) or default or anyone
        dept, matched = (pick[0] if pick else None), []
        how = "general department" if dept and dept.default else "age/gender rule"

    result: dict = {"problem": problem}
    if red_flags:
        result["possible_emergency"] = red_flags
    if dept is None:
        result["department"] = None
        result["instruction"] = "No suitable department. Offer to connect the caller to staff."
        return result
    doctors = biz.providers_in(dept.id)
    cur = biz.fees.currency
    _, days, fu = biz.fee_rules(doctors[0].id if doctors else None)
    result.update({
        "department": dept.name, "department_id": dept.id, "why": how, "matched": matched,
        "doctors": [{"provider_id": p.id, "name": p.name, "specialty": p.specialty,
                     "consultation_fee": f"{cur}{biz.fee_rules(p.id)[0]}"} for p in doctors],
        "follow_up": f"repeat visit within {days} days: " + ("free" if fu == 0 else f"{cur}{fu}"),
        "needs_age": age is None and any(d.age_min is not None or d.age_max is not None for d in biz.departments),
        "instruction": ("If possible_emergency is present and it is happening now, treat it as an emergency "
                        "(transfer_to_human with emergency=true). Otherwise tell the caller which department and "
                        "doctor to see, in one sentence, without diagnosing or naming a disease. If needs_age is "
                        "true and it matters (it could be a child), ask the patient's age first. Then offer an "
                        "OPD token or an appointment with that doctor."),
    })
    return result
