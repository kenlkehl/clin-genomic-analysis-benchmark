"""Balance question candidates to hit the target ambiguous/unambiguous mix."""

from __future__ import annotations

from .schema import Question


def balance(
    candidates: list[Question],
    target_n: int,
    target_ambiguous_frac: float = 0.5,
) -> tuple[list[Question], dict[str, int]]:
    """Pick `target_n` from candidates with the requested ambiguous fraction.

    Returns (selected, stats) where stats reports what was achieved + shortfalls.
    """
    target_amb = round(target_n * target_ambiguous_frac)
    target_unamb = target_n - target_amb

    amb = [q for q in candidates if q.classification == "ambiguous"]
    unamb = [q for q in candidates if q.classification == "unambiguous"]

    # First fill each side up to its target
    selected_amb = amb[:target_amb]
    selected_unamb = unamb[:target_unamb]

    # If short on one side, top up from the other to keep total = target_n
    deficit_amb = target_amb - len(selected_amb)
    deficit_unamb = target_unamb - len(selected_unamb)
    if deficit_amb > 0 and len(unamb) > len(selected_unamb):
        extra = unamb[len(selected_unamb): len(selected_unamb) + deficit_amb]
        selected_unamb.extend(extra)
    if deficit_unamb > 0 and len(amb) > len(selected_amb):
        extra = amb[len(selected_amb): len(selected_amb) + deficit_unamb]
        selected_amb.extend(extra)

    selected = selected_amb + selected_unamb
    stats = {
        "target_n": target_n,
        "target_ambiguous": target_amb,
        "target_unambiguous": target_unamb,
        "selected_total": len(selected),
        "selected_ambiguous": len(selected_amb),
        "selected_unambiguous": len(selected_unamb),
        "shortfall": max(0, target_n - len(selected)),
    }
    return selected, stats
