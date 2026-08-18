from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.backup import build_logical_snapshot, read_logical_snapshot, restore_logical_snapshot
from app.database import Base
from app.models import RunLog, Video


def _make_engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


def test_backup_roundtrip_preserves_rows_and_json() -> None:
    source = _make_engine()
    with Session(source) as db:
        db.add(
            Video(
                id="abc123",
                title="Example",
                description="https://example.com",
                lifetime_views=12345,
            )
        )
        db.add(RunLog(job="test", status="complete", counters={"saved": 7}))
        db.commit()

    payload = build_logical_snapshot(source)
    document = read_logical_snapshot(payload)

    assert document["format"] == "expandosaurus-logical-backup-v1"
    assert document["row_counts"]["videos"] == 1
    assert document["row_counts"]["run_logs"] == 1

    target = _make_engine()
    restore_logical_snapshot(target, payload)

    with Session(target) as db:
        video = db.scalar(select(Video).where(Video.id == "abc123"))
        run = db.scalar(select(RunLog).where(RunLog.job == "test"))
        assert video is not None
        assert video.lifetime_views == 12345
        assert run is not None
        assert run.counters == {"saved": 7}


def test_restore_refuses_nonempty_database() -> None:
    source = _make_engine()
    with Session(source) as db:
        db.add(Video(id="abc123", title="Example"))
        db.commit()
    payload = build_logical_snapshot(source)

    target = _make_engine()
    with Session(target) as db:
        db.add(Video(id="existing", title="Existing"))
        db.commit()

    try:
        restore_logical_snapshot(target, payload)
    except ValueError as exc:
        assert "non-empty" in str(exc)
    else:
        raise AssertionError("restore should refuse a non-empty target")
