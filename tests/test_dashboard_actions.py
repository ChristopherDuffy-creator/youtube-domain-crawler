import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.main import set_dashboard_decision
from app.models import DashboardDecision, Domain, Opportunity


def test_dashboard_decision_can_be_set_changed_and_toggled_off() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        domain = Domain(name="actionable.example")
        db.add(domain)
        db.flush()
        db.add(Opportunity(domain_id=domain.id, tier="qualified"))
        db.commit()

        response = set_dashboard_decision(
            system="web",
            domain_id=domain.id,
            decision_status="shortlisted",
            return_to="/?view=web&tier=qualified",
            _="admin",
            db=db,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/?view=web&tier=qualified"
        decision = db.scalar(select(DashboardDecision))
        assert decision is not None and decision.status == "shortlisted"

        set_dashboard_decision(
            system="web",
            domain_id=domain.id,
            decision_status="bought",
            return_to="/",
            _="admin",
            db=db,
        )
        decision = db.scalar(select(DashboardDecision))
        assert decision is not None and decision.status == "bought"

        set_dashboard_decision(
            system="web",
            domain_id=domain.id,
            decision_status="bought",
            return_to="/",
            _="admin",
            db=db,
        )
        assert db.scalar(select(DashboardDecision)) is None


def test_legacy_dashboard_decision_route_rejects_youtube_mutations() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        domain = Domain(name="youtube-action.example")
        db.add(domain)
        db.commit()

        with pytest.raises(HTTPException) as exc_info:
            set_dashboard_decision(
                system="youtube",
                domain_id=domain.id,
                decision_status="ignored",
                return_to="/?view=youtube",
                _="admin",
                db=db,
            )
        assert exc_info.value.status_code == 400
