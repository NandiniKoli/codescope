"""
risk.py
Turns raw impact-analysis results into a simple, human-readable risk level.
Deliberately simple rules for now -- this is the first version, meant to be
replaced later with weighted scoring (e.g. criticality of affected files).
"""


def score_risk(affected_functions):
    count = len(affected_functions)

    if count == 0:
        level = "Low"
    elif count <= 2:
        level = "Medium"
    elif count <= 10:
        level = "High"
    else:
        level = "Critical"

    return {
        "risk_level": level,
        "affected_count": count,
        "affected_functions": affected_functions,
    }