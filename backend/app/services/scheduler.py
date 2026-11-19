"""
Scheduling algorithm for bar event planning — CP-SAT solver (OR-Tools).

Only truly hard constraint: R6 — max_personnes per slot must be respected exactly
(or as close as possible if not enough available members).

Everything else is soft, expressed as weighted penalties in the objective:
  P_understaffed  — slot has fewer than max_personnes      (weight 10000)
  P_no_bureau     — slot has no bureau member              (weight 500)
  P_no_respo      — soirée slot has no respo bar           (weight 800)
  P_respo_isole   — respo covers a single slot, not a pair (weight 400)
  P_long_pause    — gap ≥3h between blocks                 (weight 200 per gap-hour)
  P_medium_pause  — gap of 2 slots                         (weight 150)
  P_small_pause   — gap of 1 slot (2h actual pause)        (weight 60)
  P_triple        — 3 consecutive slots                    (weight 500)
  P_quota_3rd_h   — 3rd hour: bureau 80, non-bureau 300
  P_quota_4th_h   — 4th hour and beyond                    (weight 600)
  P_imbalance     — difference between max and min hours   (weight 300)
  P_maybe         — member placed on a "maybe" slot        (weight 30)
  P_equite        — member has staffed more than the least-staffed eligible
                    member on past events of the same type (weight 8 per
                    delta point, capped at 10, per assigned hour)
  P_respo_rotation— respo candidate with more cumulated respo shifts than the
                    least-served candidate (weight 15 per delta point, capped
                    at 10, per respo slot)
  R_consecutive   — reward for consecutive pairs            (weight −15)

Shifts custom (ex. entrée) : postes parallèles au bar sur un créneau existant,
mêmes dispos que le créneau. Un membre est au bar OU sur un shift custom à une
heure donnée (dur). Les pénalités par membre (pauses, quota, triple, équité…)
portent sur ses heures travaillées tous postes confondus.
  P_custom_understaffed — place vide sur un shift custom   (weight 3000 < 10000 : bar d'abord)
  P_custom_no_bureau    — shift custom sans bureau          (weight 500)

The solver finds the global optimum within the time limit, then falls back
to the best solution found if the optimum is not proven.

Two entry points:
  generate_schedule  — solves once and persists the auto assignments.
  generate_proposals — solves up to N times without writing anything; each
                       new solution must differ from the previous ones by a
                       Hamming distance ≥ 3 on the assignment cells.

Warnings generated (same codes as before):
  W1 — no respo bar on a slot (soirée only)
  W2 — slot understaffed
  W3 — no bureau member on a slot
  W4 — member assigned beyond 2h base quota
  W5 — member placed despite "maybe" availability
  W6 — affinity constraint unsatisfied
  W7 — long pause (≥3h gap) between two assignment blocks
  W8 — member placed (manually) on a slot without "yes"/"maybe" availability
W2/W3 also apply to each custom shift.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from ortools.sat.python import cp_model
from sqlalchemy.orm import Session

from ..models.models import (
    AffinityConstraint,
    Assignment,
    AssignmentSourceEnum,
    AssignmentTypeEnum,
    Availability,
    Event,
    EventTypeEnum,
    Member,
    ResponsableBar,
    ShiftCustom,
    Slot,
    StatsMemberHeureRelative,
)


BASE_QUOTA = 2  # hours before the over-quota penalties kick in

# Fairness weights (see module docstring)
W_EQUITE, EQUITE_CAP = 8, 10
W_ROTATION, ROTATION_CAP = 15, 10


# ---------------------------------------------------------------------------
# Data structures (kept identical for API compatibility)
# ---------------------------------------------------------------------------


@dataclass
class WarningItem:
    type: str
    slot_id: Optional[int] = None
    member_id: Optional[int] = None
    message: str = ""


@dataclass
class _SlotAssignment:
    member_id: int
    asgn_type: str    # "staff" | "respo_bar"
    source: str       # "auto" | "manuel"
    maybe_force: bool = False
    shift_custom_id: Optional[int] = None   # None = bar


@dataclass
class _Context:
    """Everything the solver needs, loaded once from the DB."""
    event: Event
    slots: List[Slot]                          # sorted by heure_debut
    member_map: Dict[int, Member]
    avail: Dict[Tuple[int, int], str]          # (member_id, slot_idx) -> statut
    total_fois_staffe: Dict[int, int]          # per member, same event type
    affinity_set: Set[Tuple[int, int]]         # bidirectional
    respo_order: Dict[int, int]                # member_id -> ordre
    manual: Dict[Tuple[int, int], Tuple[str, Optional[int]]]
    # (member_id, slot_idx) -> (type, shift_custom_id)
    customs: List[Tuple[ShiftCustom, int]]     # (shift custom, slot_idx)
    eligible_members: List[Member]
    member_idx: Dict[int, int]                 # member_id -> index in eligible

    @property
    def n(self) -> int:
        return len(self.slots)


@dataclass
class _Solution:
    assignments: List[List[_SlotAssignment]]   # by slot idx, manual + auto
    # (member_idx, col) at 1 — col = slot_idx (bar) or n + k (custom k)
    cells: Set[Tuple[int, int]] = field(default_factory=set)
    score: int = 0


# ---------------------------------------------------------------------------
# Context loading
# ---------------------------------------------------------------------------


def _load_context(
    db: Session,
    event_id: int,
    excluded_member_ids: Sequence[int] = (),
) -> _Context:
    event: Optional[Event] = db.query(Event).filter(Event.id == event_id).first()
    if event is None:
        raise ValueError(f"Event {event_id} not found")

    slots: List[Slot] = sorted(
        db.query(Slot).filter(Slot.evenement_id == event_id).all(),
        key=lambda s: s.heure_debut,
    )
    n = len(slots)
    slot_id_to_idx: Dict[int, int] = {s.id: i for i, s in enumerate(slots)}

    all_members: List[Member] = db.query(Member).all()
    member_map: Dict[int, Member] = {m.id: m for m in all_members}

    # Availability: (member_id, slot_idx) -> "yes" | "no" | "maybe"
    avail: Dict[Tuple[int, int], str] = {}
    for av in db.query(Availability).filter(Availability.evenement_id == event_id).all():
        idx = slot_id_to_idx.get(av.creneau_id)
        if idx is not None:
            avail[(av.membre_id, idx)] = av.statut.value

    # Historical stats (same event type) — feeds P_equite
    total_fois_staffe: Dict[int, int] = {m.id: 0 for m in all_members}
    for stat in db.query(StatsMemberHeureRelative).filter(
        StatsMemberHeureRelative.type_evenement == event.type
    ).all():
        total_fois_staffe[stat.membre_id] = (
            total_fois_staffe.get(stat.membre_id, 0) + stat.fois_staffe
        )

    # Affinity pairs (bidirectional)
    affinity_set: Set[Tuple[int, int]] = set()
    for ac in db.query(AffinityConstraint).filter(
        AffinityConstraint.evenement_id == event_id
    ).all():
        affinity_set.add((ac.membre_a_id, ac.membre_b_id))
        affinity_set.add((ac.membre_b_id, ac.membre_a_id))

    # Respo bar designated order
    respo_order: Dict[int, int] = {
        r.membre_id: r.ordre
        for r in db.query(ResponsableBar).filter(
            ResponsableBar.evenement_id == event_id
        ).all()
    }

    # Manual assignments (fixed, not touched by solver)
    manual: Dict[Tuple[int, int], Tuple[str, Optional[int]]] = {}
    for slot_idx, slot in enumerate(slots):
        for a in db.query(Assignment).filter(
            Assignment.creneau_id == slot.id,
            Assignment.source == AssignmentSourceEnum.manuel,
        ).all():
            manual[(a.membre_id, slot_idx)] = (a.type.value, a.shift_custom_id)

    customs: List[Tuple[ShiftCustom, int]] = sorted(
        (
            (sc, slot_id_to_idx[sc.creneau_id])
            for sc in db.query(ShiftCustom).filter(
                ShiftCustom.evenement_id == event_id
            ).all()
            if sc.creneau_id in slot_id_to_idx
        ),
        key=lambda t: (t[1], t[0].nom),
    )

    # Members with at least one available slot (excluding explicitly excluded ones)
    excluded_set = set(excluded_member_ids)
    eligible_members = [
        m for m in all_members
        if m.id not in excluded_set
        and (
            any(avail.get((m.id, s), "no") != "no" for s in range(n))
            or any((m.id, s) in manual for s in range(n))
        )
    ]
    member_idx: Dict[int, int] = {m.id: i for i, m in enumerate(eligible_members)}

    return _Context(
        event=event,
        slots=slots,
        member_map=member_map,
        avail=avail,
        total_fois_staffe=total_fois_staffe,
        affinity_set=affinity_set,
        respo_order=respo_order,
        manual=manual,
        customs=customs,
        eligible_members=eligible_members,
        member_idx=member_idx,
    )


# ---------------------------------------------------------------------------
# Model building + solving
# ---------------------------------------------------------------------------


def _solve(
    ctx: _Context,
    *,
    max_time: float = 15.0,
    seed: Optional[int] = None,
    forbidden: Sequence[Set[Tuple[int, int]]] = (),
) -> Optional[_Solution]:
    """
    Build and solve the CP-SAT model. Pure function: no DB access, no writes.

    forbidden: cell sets ((member_idx, col) at 1, see _Solution.cells) of
    previously found solutions; each new solution must differ by a Hamming
    distance ≥ 3.
    Returns None if no feasible solution is found.
    """
    event = ctx.event
    slots = ctx.slots
    n = ctx.n
    avail = ctx.avail
    manual = ctx.manual
    eligible_members = ctx.eligible_members
    member_idx = ctx.member_idx
    M = len(eligible_members)

    model = cp_model.CpModel()

    customs = ctx.customs
    K = len(customs)
    custom_k = {sc.id: k for k, (sc, _) in enumerate(customs)}
    customs_at: Dict[int, List[int]] = {}
    for k, (_, s) in enumerate(customs):
        customs_at.setdefault(s, []).append(k)

    # x[i][s] = 1 if member i is at the bar on slot s
    x: List[List[cp_model.IntVar]] = [
        [model.new_bool_var(f"x_{i}_{s}") for s in range(n)]
        for i in range(M)
    ]
    # y[i][k] = 1 if member i is on custom shift k
    y: List[List[cp_model.IntVar]] = [
        [model.new_bool_var(f"y_{i}_{k}") for k in range(K)]
        for i in range(M)
    ]
    # w[i][s] = 1 if member i works (any post) on slot s
    w: List[List[cp_model.LinearExprT]] = [
        [x[i][s] + sum(y[i][k] for k in customs_at.get(s, [])) for s in range(n)]
        for i in range(M)
    ]
    for i in range(M):
        for s in customs_at:
            model.add(w[i][s] <= 1)  # bar OR one custom shift, never both

    # Fix manual assignments
    for (mid, s), (_, scid) in manual.items():
        if mid in member_idx:
            i = member_idx[mid]
            if scid is not None and scid in custom_k:
                model.add(y[i][custom_k[scid]] == 1)
            else:
                model.add(x[i][s] == 1)

    # Fix unavailable slots (avail == "no" and not manual)
    for i, m in enumerate(eligible_members):
        for s in range(n):
            if avail.get((m.id, s), "no") == "no" and (m.id, s) not in manual:
                model.add(w[i][s] == 0)

    def cell_var(i: int, c: int) -> cp_model.IntVar:
        return x[i][c] if c < n else y[i][c - n]

    # Exclude previously found solutions (Hamming distance ≥ 3 on cells)
    for prev in forbidden:
        diff_terms: List[cp_model.LinearExprT] = []
        for i in range(M):
            for c in range(n + K):
                v = cell_var(i, c)
                diff_terms.append(1 - v if (i, c) in prev else v)
        model.add(sum(diff_terms) >= 3)

    # ------------------------------------------------------------------
    # Penalty variables
    # ------------------------------------------------------------------
    penalties: List[cp_model.LinearExprT] = []

    # --- P_understaffed & P_overstaffed (R6) ---
    # Soft understaffing (no members available → can't do better)
    # Hard overstaffing (never exceed max_personnes)
    for s in range(n):
        assigned_on_s = [x[i][s] for i in range(M)]
        # Hard: never exceed max_personnes
        model.add(sum(assigned_on_s) <= slots[s].max_personnes)
        # Soft: penalise falling short
        shortage = model.new_int_var(0, slots[s].max_personnes, f"shortage_{s}")
        model.add(shortage == slots[s].max_personnes - sum(assigned_on_s))
        penalties.append(10000 * shortage)

    # Custom shifts: same hard cap, lighter shortage so the bar fills first
    for k, (sc, _) in enumerate(customs):
        assigned_on_k = [y[i][k] for i in range(M)]
        model.add(sum(assigned_on_k) <= sc.max_personnes)
        shortage = model.new_int_var(0, sc.max_personnes, f"c_shortage_{k}")
        model.add(shortage == sc.max_personnes - sum(assigned_on_k))
        penalties.append(3000 * shortage)

    # --- P_no_bureau (W3) — bar slots and custom shifts ---
    bureau_indices = [i for i, m in enumerate(eligible_members)
                      if m.roles and "bureau" in m.roles]
    for c in range(n + K):
        no_bureau = model.new_bool_var(f"no_bureau_{c}")
        if bureau_indices:
            # no_bureau = 1 iff all bureau members are absent from column c
            bureau_sum = sum(cell_var(i, c) for i in bureau_indices)
            # bureau_sum == 0 → no_bureau == 1
            # bureau_sum >= 1 → no_bureau == 0
            model.add(bureau_sum + no_bureau >= 1)
            model.add(bureau_sum <= len(bureau_indices) * (1 - no_bureau))
        else:
            model.add(no_bureau == 1)
        penalties.append(500 * no_bureau)

    # --- P_no_respo (W1, soirée only) ---
    if event.type == EventTypeEnum.soiree:
        respo_candidates = [
            i for i, m in enumerate(eligible_members)
            if m.id in ctx.respo_order or (m.roles and "bureau" in m.roles)
        ]
        r: Optional[List[List[cp_model.IntVar]]] = [
            [model.new_bool_var(f"r_{i}_{s}") for s in range(n)]
            for i in range(M)
        ]
        for i in range(M):
            for s in range(n):
                model.add(r[i][s] <= x[i][s])
                if i not in respo_candidates:
                    model.add(r[i][s] == 0)

        # At most 1 respo per slot
        for s in range(n):
            model.add(sum(r[i][s] for i in range(M)) <= 1)

        # At most 2h respo per member (one pair)
        for i in range(M):
            model.add(sum(r[i][s] for s in range(n)) <= 2)

        # No gaps: r[i][s]=1 and r[i][s+2]=1 implies r[i][s+1]=1
        # Prevents non-consecutive double respo (e.g. 04h + 06h without 05h)
        for i in range(M):
            for s in range(n - 2):
                model.add(r[i][s] + r[i][s + 2] <= 1 + r[i][s + 1])

        # Penalise uncovered slots (W1) — weight 800
        for s in range(n):
            no_respo = model.new_int_var(0, 1, f"no_respo_{s}")
            respo_sum = sum(r[i][s] for i in range(M))
            model.add(respo_sum + no_respo >= 1)
            model.add(respo_sum <= 1)
            penalties.append(800 * no_respo)

        # Penalise singleton respos (member covers only 1 slot instead of a 2h pair).
        # Weight 400 < 800: solver tolerates 1 singleton only when n is odd (unavoidable).
        # n even → 0 singletons (pairs cover all slots at 0 cost)
        # n odd  → exactly 1 singleton needed (400 < 800 for an uncovered slot)
        for i in range(M):
            for s in range(n):
                isolated = model.new_bool_var(f"iso_r_{i}_{s}")
                model.add(isolated <= r[i][s])
                adj: List[cp_model.IntVar] = []
                if s > 0:
                    adj.append(r[i][s - 1])
                    model.add(isolated <= 1 - r[i][s - 1])
                if s < n - 1:
                    adj.append(r[i][s + 1])
                    model.add(isolated <= 1 - r[i][s + 1])
                model.add(isolated >= r[i][s] - sum(adj))
                penalties.append(400 * isolated)

        # --- P_respo_rotation: favour candidates with the fewest cumulated
        # respo shifts. Max 15×10=150 per slot < 400 (singleton) < 800
        # (uncovered): rotation never degrades coverage.
        if respo_candidates:
            min_respo = min(
                (eligible_members[i].shifts_respo_bar_cumules or 0)
                for i in respo_candidates
            )
            for i in respo_candidates:
                cumul = eligible_members[i].shifts_respo_bar_cumules or 0
                delta = min(cumul - min_respo, ROTATION_CAP)
                if delta > 0:
                    penalties.append(
                        W_ROTATION * delta * sum(r[i][s] for s in range(n))
                    )
    else:
        r = None

    # --- P_long_pause / P_medium_pause (W7) ---
    # Penalise gaps at BLOCK BOUNDARIES only — no double counting.
    # gs[s]=1 if member assigned at s but NOT at s+1 (end of a block)
    # ge[s]=1 if member assigned at s but NOT at s-1 (start of a block)
    # Penalty fires only for pairs (s1, s2) where s1 is a block end and s2 a block start.
    for i in range(M):
        g_starts: List[cp_model.IntVar] = []
        g_ends: List[cp_model.IntVar] = []
        for s in range(n):
            gst = model.new_bool_var(f"gs_{i}_{s}")
            gen = model.new_bool_var(f"ge_{i}_{s}")
            if s < n - 1:
                model.add(gst <= w[i][s])
                model.add(gst <= 1 - w[i][s + 1])
                model.add(gst >= w[i][s] - w[i][s + 1])
            else:
                model.add(gst == w[i][s])
            if s > 0:
                model.add(gen <= w[i][s])
                model.add(gen <= 1 - w[i][s - 1])
                model.add(gen >= w[i][s] - w[i][s - 1])
            else:
                model.add(gen == w[i][s])
            g_starts.append(gst)
            g_ends.append(gen)

        for s1 in range(n - 1):
            for s2 in range(s1 + 2, n):
                gap = s2 - s1 - 1
                pair = model.new_bool_var(f"gp_{i}_{s1}_{s2}")
                model.add(pair <= g_starts[s1])
                model.add(pair <= g_ends[s2])
                model.add(pair >= g_starts[s1] + g_ends[s2] - 1)
                if gap >= 3:
                    weight = 200 * gap
                elif gap == 2:
                    weight = 150
                else:  # gap == 1 → 2h actual pause
                    weight = 60
                penalties.append(weight * pair)

    # --- R1: max 2 consecutive slots per member (soft) ---
    for i in range(M):
        for s in range(n - 2):
            triple = model.new_bool_var(f"triple_{i}_{s}")
            model.add(triple <= w[i][s])
            model.add(triple <= w[i][s + 1])
            model.add(triple <= w[i][s + 2])
            model.add(triple >= w[i][s] + w[i][s + 1] + w[i][s + 2] - 2)
            penalties.append(500 * triple)  # last resort: strongly discouraged

    # --- P_quota_exceeded (W4) ---
    # Progressive penalty:
    #   3rd hour (1st excess): bureau=80, non-bureau=300  → bureau preferred for first extension
    #   4th hour and beyond  : 600 for everyone           → strongly discourages stacking on one person
    bureau_set = set(bureau_indices)
    for i in range(M):
        hours = sum(w[i][s] for s in range(n))
        excess = model.new_int_var(0, n, f"excess_{i}")
        model.add(excess >= hours - BASE_QUOTA)
        model.add(excess >= 0)
        # First extra hour (3rd): cheap for bureau, expensive for non-bureau
        weight_first = 80 if i in bureau_set else 300
        penalties.append(weight_first * excess)
        # Additional extra hours (4th+): very expensive for everyone
        excess_beyond1 = model.new_int_var(0, n, f"excess_b1_{i}")
        model.add(excess_beyond1 >= excess - 1)
        model.add(excess_beyond1 >= 0)
        penalties.append(600 * excess_beyond1)

    # --- P_imbalance ---
    hours_vars = []
    for i in range(M):
        h = model.new_int_var(0, n, f"hours_{i}")
        model.add(h == sum(w[i][s] for s in range(n)))
        hours_vars.append(h)
    max_h = model.new_int_var(0, n, "max_h")
    min_h = model.new_int_var(0, n, "min_h")
    model.add_max_equality(max_h, hours_vars)
    model.add_min_equality(min_h, hours_vars)
    imbalance = model.new_int_var(0, n, "imbalance")
    model.add(imbalance == max_h - min_h)
    penalties.append(300 * imbalance)

    # --- P_maybe ---
    for i, m in enumerate(eligible_members):
        for s in range(n):
            if avail.get((m.id, s), "no") == "maybe" and (m.id, s) not in manual:
                penalties.append(30 * w[i][s])

    # --- P_equite: favour members who have staffed less on past events of the
    # same type. Tie-breaker scale (8×cap 10 = max 80/h): below the structural
    # penalties, above the consecutive-pair reward.
    if M:
        min_staffe = min(
            ctx.total_fois_staffe.get(m.id, 0) for m in eligible_members
        )
        for i, m in enumerate(eligible_members):
            delta = min(
                ctx.total_fois_staffe.get(m.id, 0) - min_staffe, EQUITE_CAP
            )
            if delta > 0:
                penalties.append(
                    W_EQUITE * delta * sum(w[i][s] for s in range(n))
                )

    # --- P_no_2h_block: prefer adjacent placements (soft R4) ---
    # Reward consecutive pairs: subtract penalty for each consecutive pair
    for i in range(M):
        for s in range(n - 1):
            consec = model.new_bool_var(f"consec_{i}_{s}")
            model.add(consec <= w[i][s])
            model.add(consec <= w[i][s + 1])
            model.add(consec >= w[i][s] + w[i][s + 1] - 1)
            penalties.append(-15 * consec)  # negative = reward

    # ------------------------------------------------------------------
    # Objective: minimise total penalty
    # ------------------------------------------------------------------
    model.minimize(sum(penalties))

    # ------------------------------------------------------------------
    # Solve
    # ------------------------------------------------------------------
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max_time
    solver.parameters.num_search_workers = 4
    if seed is not None:
        solver.parameters.random_seed = seed
    status = solver.solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None

    # ------------------------------------------------------------------
    # Extract solution
    # ------------------------------------------------------------------
    assignments: List[List[_SlotAssignment]] = [[] for _ in range(n)]
    cells: Set[Tuple[int, int]] = set()

    # Manual assignments first
    for (mid, s), (atype, scid) in manual.items():
        av_status = avail.get((mid, s), "no")
        if scid not in custom_k:
            scid = None
        assignments[s].append(_SlotAssignment(
            member_id=mid,
            asgn_type=atype,
            source="manuel",
            maybe_force=(av_status == "maybe"),
            shift_custom_id=scid,
        ))
        if mid in member_idx:
            col = s if scid is None else n + custom_k[scid]
            cells.add((member_idx[mid], col))

    # Auto assignments from solver
    respo_assigned: Set[Tuple[int, int]] = set()
    if r is not None:
        for i in range(M):
            m = eligible_members[i]
            for s in range(n):
                if solver.value(r[i][s]) == 1:
                    respo_assigned.add((m.id, s))

    for i, m in enumerate(eligible_members):
        for s in range(n):
            if solver.value(x[i][s]) == 1:
                cells.add((i, s))
                if (m.id, s) in manual:
                    continue
                atype = "respo_bar" if (m.id, s) in respo_assigned else "staff"
                av_status = avail.get((m.id, s), "no")
                assignments[s].append(_SlotAssignment(
                    member_id=m.id,
                    asgn_type=atype,
                    source="auto",
                    maybe_force=(av_status == "maybe"),
                ))
        for k, (sc, s) in enumerate(customs):
            if solver.value(y[i][k]) == 1:
                cells.add((i, n + k))
                if (m.id, s) in manual:
                    continue
                assignments[s].append(_SlotAssignment(
                    member_id=m.id,
                    asgn_type="staff",
                    source="auto",
                    maybe_force=(avail.get((m.id, s), "no") == "maybe"),
                    shift_custom_id=sc.id,
                ))

    return _Solution(
        assignments=assignments,
        cells=cells,
        score=int(solver.objective_value),
    )


def _replace_auto_assignments(
    db: Session,
    slots: List[Slot],
    assignments: List[List[_SlotAssignment]],
) -> None:
    """Delete previous auto assignments and persist the new ones."""
    for slot in slots:
        db.query(Assignment).filter(
            Assignment.creneau_id == slot.id,
            Assignment.source == AssignmentSourceEnum.auto,
        ).delete(synchronize_session=False)
    db.flush()

    for slot_idx, slot in enumerate(slots):
        for a in assignments[slot_idx]:
            if a.source != "auto":
                continue
            db.add(Assignment(
                creneau_id=slot.id,
                membre_id=a.member_id,
                type=AssignmentTypeEnum(a.asgn_type),
                source=AssignmentSourceEnum.auto,
                maybe_force=a.maybe_force,
                shift_custom_id=a.shift_custom_id,
            ))
    db.commit()


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def generate_schedule(
    db: Session,
    event_id: int,
    excluded_member_ids: List[int] = [],
) -> List[WarningItem]:
    """
    Generate (or regenerate) the automatic schedule for an event.
    Clears existing *auto* assignments and replaces them.
    Returns a list of WarningItem.

    excluded_member_ids: members to skip entirely (not assigned by the solver,
    but their manual assignments are kept).
    """
    ctx = _load_context(db, event_id, excluded_member_ids)
    if not ctx.slots:
        return [WarningItem(type="no_slots", message="Aucun créneau configuré")]

    solution = _solve(ctx, max_time=15.0)
    if solution is None:
        return [WarningItem(
            type="solver_failed",
            message="Le solveur n'a pas trouvé de solution. Vérifiez les disponibilités.",
        )]

    _replace_auto_assignments(db, ctx.slots, solution.assignments)

    return _generate_warnings(ctx, solution.assignments)


def generate_proposals(
    db: Session,
    event_id: int,
    excluded_member_ids: List[int] = [],
    count: int = 3,
) -> List[dict]:
    """
    Solve up to `count` times without writing anything to the DB.
    Each solution must differ from the previous ones (Hamming distance ≥ 3
    on the assignment cells); stops early when no distinct solution remains.

    Returns a list of proposals:
      {index, score, warnings: List[WarningItem],
       assignments: List[{creneau_id, membre_id, type, source, maybe_force}]}
    """
    ctx = _load_context(db, event_id, excluded_member_ids)
    if not ctx.slots:
        return []

    proposals: List[dict] = []
    forbidden: List[Set[Tuple[int, int]]] = []

    for k in range(count):
        solution = _solve(ctx, max_time=6.0, seed=1000 + k, forbidden=forbidden)
        if solution is None:
            break

        flat: List[dict] = []
        for slot_idx, slot in enumerate(ctx.slots):
            for a in solution.assignments[slot_idx]:
                flat.append({
                    "creneau_id": slot.id,
                    "membre_id": a.member_id,
                    "type": a.asgn_type,
                    "source": a.source,
                    "maybe_force": a.maybe_force,
                    "shift_custom_id": a.shift_custom_id,
                })

        proposals.append({
            "index": k,
            "score": solution.score,
            "warnings": _generate_warnings(ctx, solution.assignments),
            "assignments": flat,
        })
        forbidden.append(solution.cells)

    return proposals


# ---------------------------------------------------------------------------
# Warning generation
# ---------------------------------------------------------------------------


def _generate_warnings(
    ctx: _Context,
    assignments: List[List[_SlotAssignment]],
) -> List[WarningItem]:
    event, slots, member_map = ctx.event, ctx.slots, ctx.member_map
    warnings: List[WarningItem] = []
    slot_indices_by_member: Dict[int, Set[int]] = {}
    # (slot_idx, shift_custom_id) — W6 : ensemble = même heure, même poste
    posts_by_member: Dict[int, Set[Tuple[int, Optional[int]]]] = {}

    def has_bureau(asgns: List[_SlotAssignment]) -> bool:
        return any(
            "bureau" in (member_map[a.member_id].roles or [])
            for a in asgns if a.member_id in member_map
        )

    def member_name(mid: int) -> str:
        m = member_map.get(mid)
        return f"{m.prenom} {m.nom}" if m else str(mid)

    for slot_idx, slot in enumerate(slots):
        all_asgns = assignments[slot_idx]
        asgns = [a for a in all_asgns if a.shift_custom_id is None]  # bar
        hhmm = slot.heure_debut.strftime('%H:%M')

        # W1
        if event.type == EventTypeEnum.soiree:
            if not any(a.asgn_type == "respo_bar" for a in asgns):
                warnings.append(WarningItem(
                    type="no_respo_bar",
                    slot_id=slot.id,
                    message=f"Créneau {slot.heure_debut.strftime('%H:%M')} : aucun respo bar (W1)",
                ))

        # W2
        if len(asgns) < slot.max_personnes:
            warnings.append(WarningItem(
                type="slot_understaffed",
                slot_id=slot.id,
                message=(
                    f"Créneau {slot.heure_debut.strftime('%H:%M')} : "
                    f"{len(asgns)}/{slot.max_personnes} personnes (W2)"
                ),
            ))

        # W3
        if asgns and not has_bureau(asgns):
            warnings.append(WarningItem(
                type="no_bureau",
                slot_id=slot.id,
                message=f"Créneau {slot.heure_debut.strftime('%H:%M')} : aucun membre bureau (W3)",
            ))

        # W2/W3 per custom shift on this slot
        for sc, s in ctx.customs:
            if s != slot_idx:
                continue
            on_sc = [a for a in all_asgns if a.shift_custom_id == sc.id]
            if len(on_sc) < sc.max_personnes:
                warnings.append(WarningItem(
                    type="slot_understaffed",
                    slot_id=slot.id,
                    message=(
                        f"{sc.nom} {hhmm} : "
                        f"{len(on_sc)}/{sc.max_personnes} personnes (W2)"
                    ),
                ))
            if not has_bureau(on_sc):
                warnings.append(WarningItem(
                    type="no_bureau",
                    slot_id=slot.id,
                    message=f"{sc.nom} {hhmm} : aucun membre bureau (W3)",
                ))

        for a in all_asgns:
            # W5
            if a.maybe_force:
                warnings.append(WarningItem(
                    type="maybe_forced",
                    slot_id=slot.id,
                    member_id=a.member_id,
                    message=f"Créneau {hhmm} : {member_name(a.member_id)} forcé (maybe) — W5",
                ))
            # W8
            if ctx.avail.get((a.member_id, slot_idx), "no") == "no":
                warnings.append(WarningItem(
                    type="unavailable_forced",
                    slot_id=slot.id,
                    member_id=a.member_id,
                    message=(
                        f"Créneau {hhmm} : {member_name(a.member_id)} "
                        "affecté sans être dispo — W8"
                    ),
                ))
            slot_indices_by_member.setdefault(a.member_id, set()).add(slot_idx)
            posts_by_member.setdefault(a.member_id, set()).add(
                (slot_idx, a.shift_custom_id)
            )

    # W4
    for m in ctx.eligible_members:
        total_h = len(slot_indices_by_member.get(m.id, set()))
        if total_h > BASE_QUOTA:
            warnings.append(WarningItem(
                type="over_quota",
                member_id=m.id,
                message=f"{m.prenom} {m.nom} : {total_h}h affectées (palier 2) — W4",
            ))

    # W6
    processed: Set[frozenset] = set()
    for a_id, b_id in ctx.affinity_set:
        pair = frozenset([a_id, b_id])
        if pair in processed:
            continue
        processed.add(pair)
        a_slots = posts_by_member.get(a_id, set())
        b_slots = posts_by_member.get(b_id, set())
        if not (a_slots & b_slots):
            m_a = member_map.get(a_id)
            m_b = member_map.get(b_id)
            name_a = f"{m_a.prenom} {m_a.nom}" if m_a else str(a_id)
            name_b = f"{m_b.prenom} {m_b.nom}" if m_b else str(b_id)
            warnings.append(WarningItem(
                type="affinity_unsatisfied",
                message=f"Affinité non satisfaite : {name_a} & {name_b} — W6",
            ))

    # W7
    for member_id, slot_idxs in slot_indices_by_member.items():
        sorted_idxs = sorted(slot_idxs)
        # Build blocks
        blocks: List[List[int]] = [[sorted_idxs[0]]]
        for idx in sorted_idxs[1:]:
            if idx == blocks[-1][-1] + 1:
                blocks[-1].append(idx)
            else:
                blocks.append([idx])
        for k in range(len(blocks) - 1):
            gap = blocks[k + 1][0] - blocks[k][-1] - 1
            if gap >= 3:
                m = member_map.get(member_id)
                name = f"{m.prenom} {m.nom}" if m else str(member_id)
                warnings.append(WarningItem(
                    type="long_pause",
                    member_id=member_id,
                    message=f"{name} : pause de {gap}h entre deux blocs — W7",
                ))

    return warnings
