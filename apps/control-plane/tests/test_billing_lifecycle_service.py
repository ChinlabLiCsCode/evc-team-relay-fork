"""Tests for billing_lifecycle_service — offer §13.3 post-cancellation deletion.

No SMTP call happens on this path (queue_email() only inserts a DB row), so
these tests use the real EmailService + a real SQLite session rather than
mocking the DB layer, per repo convention.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import get_settings
from app.db.models import EmailQueue, LifecycleState, Share, ShareKind, ShareVisibility, User
from app.services.billing_lifecycle_service import (
    TRIGGER_CANCELLATION_DELETION,
    cancel_scheduled_deletion,
    process_cancellation_deletions,
    schedule_cancellation_deletion,
)


@pytest.fixture(autouse=True)
def _deletion_enabled():
    os.environ["BILLING_CANCELLATION_DATA_DELETION_ENABLED"] = "true"
    os.environ["BILLING_CANCELLATION_DATA_DELETION_DAYS"] = "30"
    get_settings.cache_clear()
    yield
    os.environ.pop("BILLING_CANCELLATION_DATA_DELETION_ENABLED", None)
    os.environ.pop("BILLING_CANCELLATION_DATA_DELETION_DAYS", None)
    get_settings.cache_clear()


def _make_user(db_session, email="cancelled-user@example.com"):
    user = User(email=email, password_hash="x", is_admin=False, is_active=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _make_share(db_session, owner, path="/notes/foo"):
    share = Share(
        kind=ShareKind.FOLDER,
        path=path,
        visibility=ShareVisibility.PRIVATE,
        owner_user_id=owner.id,
    )
    db_session.add(share)
    db_session.commit()
    db_session.refresh(share)
    return share


def _state_row(db_session, user_id):
    return (
        db_session.query(LifecycleState)
        .filter_by(user_id=user_id, trigger_key=TRIGGER_CANCELLATION_DELETION)
        .one_or_none()
    )


def _aware(dt):
    """SQLite drops tzinfo on round-trip even for DateTime(timezone=True)
    columns — normalize to aware UTC before comparing against datetime.now()."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class TestScheduleCancellationDeletion:
    @pytest.mark.asyncio
    async def test_schedules_deletion_and_queues_warning_email(self, db_session):
        user = _make_user(db_session)

        await schedule_cancellation_deletion(db_session, user)

        state = _state_row(db_session, user.id)
        assert state is not None
        assert state.state == "pending"
        assert _aware(state.scheduled_at) > datetime.now(timezone.utc) + timedelta(days=29)
        assert _aware(state.scheduled_at) < datetime.now(timezone.utc) + timedelta(days=31)

        emails = db_session.query(EmailQueue).filter_by(to_email=user.email).all()
        assert len(emails) == 1
        assert emails[0].email_type == "billing_cancellation_deletion_scheduled"

    @pytest.mark.asyncio
    async def test_second_cancellation_while_pending_does_not_reset_clock_or_resend(
        self, db_session
    ):
        """A duplicate/replayed cancellation webhook must not push the deletion
        date further out or send a second warning — the whole point of an
        idempotency-keyed tracker."""
        user = _make_user(db_session)

        await schedule_cancellation_deletion(db_session, user)
        first_deadline = _state_row(db_session, user.id).scheduled_at

        await schedule_cancellation_deletion(db_session, user)

        state = _state_row(db_session, user.id)
        assert state.scheduled_at == first_deadline
        assert db_session.query(EmailQueue).filter_by(to_email=user.email).count() == 1

    @pytest.mark.asyncio
    async def test_noop_when_feature_flag_disabled(self, db_session):
        os.environ["BILLING_CANCELLATION_DATA_DELETION_ENABLED"] = "false"
        get_settings.cache_clear()
        user = _make_user(db_session)

        await schedule_cancellation_deletion(db_session, user)

        assert _state_row(db_session, user.id) is None
        assert db_session.query(EmailQueue).count() == 0


class TestCancelScheduledDeletion:
    @pytest.mark.asyncio
    async def test_resubscribe_aborts_pending_deletion(self, db_session):
        user = _make_user(db_session)
        await schedule_cancellation_deletion(db_session, user)

        await cancel_scheduled_deletion(db_session, user)

        state = _state_row(db_session, user.id)
        assert state.state == "cancelled"

    @pytest.mark.asyncio
    async def test_noop_when_nothing_pending(self, db_session):
        user = _make_user(db_session)

        await cancel_scheduled_deletion(db_session, user)

        assert _state_row(db_session, user.id) is None

    @pytest.mark.asyncio
    async def test_recancelling_after_a_resubscribe_reschedules_fresh(self, db_session):
        """cancelled -> resubscribed -> cancelled again must re-arm the same
        row (unique on user_id+trigger_key) rather than error or silently
        stay 'cancelled' forever."""
        user = _make_user(db_session)
        await schedule_cancellation_deletion(db_session, user)
        await cancel_scheduled_deletion(db_session, user)
        assert _state_row(db_session, user.id).state == "cancelled"

        await schedule_cancellation_deletion(db_session, user)

        state = _state_row(db_session, user.id)
        assert state.state == "pending"
        assert _aware(state.scheduled_at) > datetime.now(timezone.utc) + timedelta(days=29)


class TestProcessCancellationDeletions:
    @pytest.mark.asyncio
    async def test_deletes_shares_for_due_users_and_sends_confirmation(self, db_session):
        user = _make_user(db_session)
        share = _make_share(db_session, user)
        await schedule_cancellation_deletion(db_session, user)
        # Force the schedule into the past so this cycle picks it up.
        state = _state_row(db_session, user.id)
        state.scheduled_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db_session.commit()

        executed = await process_cancellation_deletions(db_session)

        assert executed == 1
        assert db_session.get(Share, share.id) is None
        assert _state_row(db_session, user.id).state == "executed"
        confirmation = (
            db_session.query(EmailQueue)
            .filter_by(to_email=user.email, email_type="billing_cancellation_deletion_executed")
            .one_or_none()
        )
        assert confirmation is not None

    @pytest.mark.asyncio
    async def test_not_yet_due_is_left_alone(self, db_session):
        user = _make_user(db_session)
        _make_share(db_session, user)
        await schedule_cancellation_deletion(db_session, user)

        executed = await process_cancellation_deletions(db_session)

        assert executed == 0
        assert _state_row(db_session, user.id).state == "pending"
        assert db_session.query(Share).count() == 1

    @pytest.mark.asyncio
    async def test_cancelled_schedule_is_never_executed(self, db_session):
        """The core safety property: a user who resubscribed before the
        deadline must never have their data deleted, even if the worker
        cycle runs after the original deadline would have passed."""
        user = _make_user(db_session)
        _make_share(db_session, user)
        await schedule_cancellation_deletion(db_session, user)
        state = _state_row(db_session, user.id)
        state.scheduled_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db_session.commit()

        await cancel_scheduled_deletion(db_session, user)
        executed = await process_cancellation_deletions(db_session)

        assert executed == 0
        assert db_session.query(Share).count() == 1
        assert _state_row(db_session, user.id).state == "cancelled"

    @pytest.mark.asyncio
    async def test_noop_when_feature_flag_disabled(self, db_session):
        user = _make_user(db_session)
        _make_share(db_session, user)
        await schedule_cancellation_deletion(db_session, user)
        state = _state_row(db_session, user.id)
        state.scheduled_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db_session.commit()

        os.environ["BILLING_CANCELLATION_DATA_DELETION_ENABLED"] = "false"
        get_settings.cache_clear()

        executed = await process_cancellation_deletions(db_session)

        assert executed == 0
        assert db_session.query(Share).count() == 1
