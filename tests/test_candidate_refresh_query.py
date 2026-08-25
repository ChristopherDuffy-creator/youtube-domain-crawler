from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.database import Base
from app.jobs import refresh_candidates
from app.models import Candidate, Domain


def test_candidate_rejection_uses_a_correlated_exists_check() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    statements: list[str] = []

    def capture_sql(*args: object) -> None:
        statements.append(str(args[2]))

    event.listen(engine, "before_cursor_execute", capture_sql)
    with Session(engine) as db:
        domain = Domain(name="inactive.example")
        db.add(domain)
        db.flush()
        db.add(Candidate(domain_id=domain.id, tier="pending"))
        db.commit()

        assert refresh_candidates(db, {domain.id}) == 0

    stale_updates = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("UPDATE CANDIDATES SET TIER")
    ]
    assert stale_updates
    assert all("NOT IN (SELECT" not in statement.upper() for statement in stale_updates)
    assert any("EXISTS" in statement.upper() for statement in stale_updates)
