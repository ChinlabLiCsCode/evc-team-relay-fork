"""Billing-cancellation data-retention lifecycle (offer §13.3).

Schedules deletion of a user's owned shares N days after their subscription
cancels/expires, aborts the schedule if they resubscribe in time, and
executes it once due.

Reuses LifecycleState — the same idempotency-keyed (user_id, trigger_key)
tracker the T1/T2 email-nudge engine in lifecycle_service.py already uses —
rather than a new table. The row shape (state + scheduled_at + sent_at)
already fits a scheduled action; this trigger just has three states
(pending/cancelled/executed) instead of two (sent/suppressed).

Gated by billing_cancellation_data_deletion_enabled (default off): this is
an irreversible destructive action on user content and must be turned on
deliberately per deployment, not inherited from billing_enabled.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import LifecycleState, Share, User
from app.services import share_service
from app.services.email_service import get_email_service

logger = get_logger(__name__)

TRIGGER_CANCELLATION_DELETION = "billing_cancellation_deletion"


def _get_state_row(db: Session, user_id) -> LifecycleState | None:
    return db.execute(
        select(LifecycleState).where(
            LifecycleState.user_id == user_id,
            LifecycleState.trigger_key == TRIGGER_CANCELLATION_DELETION,
        )
    ).scalar_one_or_none()


async def schedule_cancellation_deletion(db: Session, user: User) -> None:
    """Called on subscription.cancelled/.expired: schedule deletion + warn by email.

    Idempotent per user: a second cancellation webhook while a deletion is
    already pending is a no-op — it doesn't push the date out or resend the
    warning. A prior "cancelled" (resubscribed in time) or "executed" row is
    re-armed rather than duplicated, since (user_id, trigger_key) is unique.
    """
    settings = get_settings()
    if not settings.billing_cancellation_data_deletion_enabled:
        return

    existing = _get_state_row(db, user.id)
    if existing is not None and existing.state == "pending":
        return

    deletion_date = datetime.now(timezone.utc) + timedelta(
        days=settings.billing_cancellation_data_deletion_days
    )

    if existing is not None:
        existing.state = "pending"
        existing.scheduled_at = deletion_date
        existing.sent_at = None
        existing.suppressed_reason = None
        db.add(existing)
    else:
        db.add(
            LifecycleState(
                user_id=user.id,
                trigger_key=TRIGGER_CANCELLATION_DELETION,
                state="pending",
                scheduled_at=deletion_date,
            )
        )

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.warning(
            "Cancellation-deletion already scheduled (race), skipping",
            extra={"user_id": str(user.id)},
        )
        return

    email_svc = get_email_service()
    await email_svc.send_billing_cancellation_deletion_scheduled(
        db,
        user.email,
        deletion_date,
        settings.billing_cancellation_data_deletion_days,
    )

    logger.info(
        "Cancellation-deletion scheduled",
        extra={"user_id": str(user.id), "deletion_date": deletion_date.isoformat()},
    )


async def cancel_scheduled_deletion(db: Session, user: User) -> None:
    """Called on a webhook implying an active subscription (created/activated/
    renewed/updated): abort a pending deletion if the user resubscribed.
    """
    settings = get_settings()
    if not settings.billing_cancellation_data_deletion_enabled:
        return

    row = _get_state_row(db, user.id)
    if row is None or row.state != "pending":
        return

    row.state = "cancelled"
    db.add(row)
    db.commit()

    logger.info(
        "Cancellation-deletion aborted (resubscribed)",
        extra={"user_id": str(user.id)},
    )


async def process_cancellation_deletions(db: Session) -> int:
    """Worker cycle: execute any deletion whose scheduled_at is due.

    Returns the number of users whose data was deleted this cycle.
    """
    settings = get_settings()
    if not settings.billing_cancellation_data_deletion_enabled:
        return 0

    now = datetime.now(timezone.utc)
    due = (
        db.execute(
            select(LifecycleState).where(
                LifecycleState.trigger_key == TRIGGER_CANCELLATION_DELETION,
                LifecycleState.state == "pending",
                LifecycleState.scheduled_at <= now,
            )
        )
        .scalars()
        .all()
    )

    email_svc = get_email_service()
    executed = 0

    for row in due:
        user = db.get(User, row.user_id)
        if user is None:
            # Account itself is already gone via some other path — nothing left to do.
            row.state = "executed"
            row.sent_at = now
            db.add(row)
            db.commit()
            continue

        shares = db.execute(select(Share).where(Share.owner_user_id == user.id)).scalars().all()
        for share in shares:
            # Same deletion path the user-facing DELETE /shares/{id} endpoint
            # uses (share_service.delete_share) — not a separate reimplementation.
            share_service.delete_share(db, share, actor_user_id=None)

        row.state = "executed"
        row.sent_at = now
        db.add(row)
        db.commit()

        await email_svc.send_billing_cancellation_deletion_executed(db, user.email)

        logger.info(
            "Cancellation-deletion executed",
            extra={"user_id": str(user.id), "shares_deleted": len(shares)},
        )
        executed += 1

    return executed
