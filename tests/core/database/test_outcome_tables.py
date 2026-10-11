"""The outcome tables: a deleted outcome must not leave links and events behind."""
from core.database import Outcome, OutcomeEvent, OutcomeLink


def test_deleting_an_outcome_cascades_to_its_links_and_events(app_db):
    """Orphaned links would make `find_by_link` match a chat to a deleted outcome."""
    db = app_db.SessionLocal()
    try:
        db.add(Outcome(id="o1", owner="alice", title="Renew passport", kind="obligation",
                       state="open", source="person"))
        db.add(Outcome(id="o2", owner="alice", title="Other", kind="project",
                       state="open", source="person"))
        db.add(OutcomeLink(id="l1", outcome_id="o1", kind="todoist_task", ref="123"))
        db.add(OutcomeLink(id="l2", outcome_id="o2", kind="todoist_task", ref="123"))
        db.add(OutcomeEvent(id="e1", outcome_id="o1", actor="person", kind="created"))
        db.commit()

        db.delete(db.get(Outcome, "o1"))
        db.commit()

        assert db.query(OutcomeLink).filter_by(outcome_id="o1").count() == 0
        assert db.query(OutcomeEvent).filter_by(outcome_id="o1").count() == 0
        assert [row.id for row in db.query(OutcomeLink).all()] == ["l2"]
    finally:
        db.close()


def test_the_same_link_cannot_be_added_twice_to_one_outcome(app_db):
    """Two chat turns linking the same Todoist task must not double-count it on Today."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    db = app_db.SessionLocal()
    try:
        db.add(Outcome(id="o1", owner="alice", title="t", kind="obligation", state="open", source="person"))
        db.add(OutcomeLink(id="l1", outcome_id="o1", kind="chat", ref="s1"))
        db.commit()
        db.add(OutcomeLink(id="l2", outcome_id="o1", kind="chat", ref="s1"))
        with pytest.raises(IntegrityError):
            db.commit()
    finally:
        db.rollback()
        db.close()
