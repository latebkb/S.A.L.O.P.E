"""Shifts custom (ex. entrée) : solveur, warnings, API, affectation malgré dispo « no »."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.models import (
    Assignment,
    AssignmentSourceEnum,
    Availability,
    AvailabilityStatusEnum,
    Event,
    EventTypeEnum,
    Member,
    ShiftCustom,
    Slot,
)
from app.services.scheduler import generate_proposals, generate_schedule


def _setup(db: Session, n_members: int, bar_max: int = 1, entree_max: int = 1,
           bureau: int = 0, n_slots: int = 2):
    event = Event(nom="Shifts", date=datetime(2026, 6, 1, 20), type=EventTypeEnum.afterwork)
    db.add(event)
    db.flush()
    slots = []
    for i in range(n_slots):
        s = Slot(
            evenement_id=event.id,
            heure_debut=datetime(2026, 6, 1, 20 + i),
            heure_fin=datetime(2026, 6, 1, 21 + i),
            heure_relative=i,
            max_personnes=bar_max,
        )
        db.add(s)
        slots.append(s)
    db.flush()
    shifts = []
    for s in slots:
        sc = ShiftCustom(evenement_id=event.id, creneau_id=s.id, nom="Entrée",
                         max_personnes=entree_max)
        db.add(sc)
        shifts.append(sc)
    members = []
    for i in range(n_members):
        m = Member(nom="T", prenom=f"M{i}",
                   roles=["staff", "bureau"] if i < bureau else ["staff"],
                   shifts_respo_bar_cumules=0)
        db.add(m)
        members.append(m)
    db.flush()
    for m in members:
        for s in slots:
            db.add(Availability(evenement_id=event.id, membre_id=m.id, creneau_id=s.id,
                                statut=AvailabilityStatusEnum.yes))
    db.flush()
    return event, slots, shifts, members


def _asgns(db: Session, slots):
    return db.query(Assignment).filter(
        Assignment.creneau_id.in_([s.id for s in slots])
    ).all()


def test_custom_filled_and_never_double_booked(db_session: Session):
    event, slots, shifts, _ = _setup(db_session, n_members=4, bureau=4)
    generate_schedule(db_session, event.id)
    asgns = _asgns(db_session, slots)
    # uq_assignment : une seule ligne par (créneau, membre) = jamais bar + entrée
    for sc, s in zip(shifts, slots):
        assert sum(a.shift_custom_id == sc.id for a in asgns) == 1
        assert sum(a.creneau_id == s.id and a.shift_custom_id is None for a in asgns) == 1


def test_bar_filled_first_when_short(db_session: Session):
    # 1 seul membre dispo : il doit aller au bar, entrée vide + W2
    event, slots, shifts, _ = _setup(db_session, n_members=1, bureau=1, n_slots=1)
    warnings = generate_schedule(db_session, event.id)
    asgns = _asgns(db_session, slots)
    assert len(asgns) == 1 and asgns[0].shift_custom_id is None
    assert any(w.type == "slot_understaffed" and "Entrée" in w.message for w in warnings)


def test_custom_no_bureau_warning(db_session: Session):
    event, slots, _, _ = _setup(db_session, n_members=2, bureau=0, n_slots=1)
    warnings = generate_schedule(db_session, event.id)
    assert any(w.type == "no_bureau" and "Entrée" in w.message for w in warnings)


def test_manual_custom_kept_and_proposals(db_session: Session):
    event, slots, shifts, members = _setup(db_session, n_members=4, bureau=2)
    db_session.add(Assignment(creneau_id=slots[0].id, membre_id=members[3].id,
                              shift_custom_id=shifts[0].id,
                              source=AssignmentSourceEnum.manuel))
    db_session.flush()
    generate_schedule(db_session, event.id)
    kept = db_session.query(Assignment).filter_by(
        creneau_id=slots[0].id, membre_id=members[3].id).one()
    assert kept.shift_custom_id == shifts[0].id and kept.source == AssignmentSourceEnum.manuel

    proposals = generate_proposals(db_session, event.id, count=2)
    assert proposals
    assert any(a["shift_custom_id"] == shifts[1].id for a in proposals[0]["assignments"])


def test_api_crud_and_assignment_checks(client, db_session: Session):
    event, slots, _, members = _setup(db_session, n_members=1)
    db_session.query(ShiftCustom).delete()
    db_session.flush()
    base = f"/api/events/{event.id}"

    res = client.post(f"{base}/shifts-custom",
                      json={"nom": "Vestiaire", "creneau_ids": [s.id for s in slots],
                            "max_personnes": 2})
    assert res.status_code == 201 and len(res.json()) == 2
    sid0, sid1 = (r["id"] for r in sorted(res.json(), key=lambda r: r["creneau_id"]))

    assert client.put(f"{base}/shifts-custom/{sid0}", json={"max_personnes": 3}).json()["max_personnes"] == 3

    # shift d'un autre créneau → 422
    bad = {"creneau_id": slots[0].id, "membre_id": members[0].id, "shift_custom_id": sid1}
    assert client.post(f"{base}/assignments", json=bad).status_code == 422

    ok = {"creneau_id": slots[0].id, "membre_id": members[0].id,
          "shift_custom_id": sid0, "type": "respo_bar"}
    res = client.post(f"{base}/assignments", json=ok)
    assert res.status_code == 201
    assert res.json()["shift_custom_id"] == sid0 and res.json()["type"] == "staff"
    assert client.get(f"{base}/export-png").status_code == 200

    # retour au bar via PUT null explicite
    res = client.put(f"{base}/assignments/{res.json()['id']}", json={"shift_custom_id": None})
    assert res.json()["shift_custom_id"] is None

    assert client.delete(f"{base}/shifts-custom/{sid1}").status_code == 204
    assert len(client.get(f"{base}/shifts-custom").json()) == 1


def test_manual_assignment_despite_no_dispo(client, db_session: Session):
    event, slots, _, members = _setup(db_session, n_members=1, n_slots=1)
    av = db_session.query(Availability).filter_by(membre_id=members[0].id).one()
    av.statut = AvailabilityStatusEnum.no
    db_session.flush()

    res = client.post(f"/api/events/{event.id}/assignments",
                      json={"creneau_id": slots[0].id, "membre_id": members[0].id})
    assert res.status_code == 201

    warnings = generate_schedule(db_session, event.id)
    assert any(w.type == "unavailable_forced" and w.member_id == members[0].id
               for w in warnings)
    assert db_session.query(Assignment).filter_by(membre_id=members[0].id).count() == 1


def test_delete_event_with_custom_assignments(client, db_session: Session):
    import warnings as pywarnings
    event, _, _, _ = _setup(db_session, n_members=4, bureau=2)
    generate_schedule(db_session, event.id)
    with pywarnings.catch_warnings():
        pywarnings.simplefilter("error")
        assert client.delete(f"/api/events/{event.id}").status_code == 204
