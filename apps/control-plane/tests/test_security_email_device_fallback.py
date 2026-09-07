"""Device-name fallback for the "new login" security email (#228403f8 follow-up).

`device_name` comes from the optional `X-Device-Name` header, which none of
our shipped clients (curl, the admin web UI, the Obsidian plugin) send — so
every real alert used to show a fixed "Unknown device" placeholder even
though the request's User-Agent was known and queued right alongside it.
send_security_new_session now falls back to user_agent before the fixed
string.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import security
from app.db.models import EmailQueue, User
from app.services.email_service import EMAIL_TYPE_SECURITY_NEW_SESSION, EmailService


def make_user(db: Session, email: str = "device-fallback@example.com") -> User:
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=security.get_password_hash("test123456"),
        is_admin=False,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def latest_session_email(db: Session, to_email: str) -> EmailQueue:
    stmt = (
        select(EmailQueue)
        .where(
            EmailQueue.to_email == to_email,
            EmailQueue.email_type == EMAIL_TYPE_SECURITY_NEW_SESSION,
        )
        .order_by(EmailQueue.id.desc())
    )
    return db.execute(stmt).scalars().first()


@pytest.mark.asyncio
class TestSecurityNewSessionDeviceFallback:
    async def test_no_device_name_falls_back_to_user_agent(self, db_session: Session):
        user = make_user(db_session)
        service = EmailService()

        await service.send_security_new_session(
            db_session,
            user.email,
            user.id,
            device_name=None,
            ip_address="203.0.113.5",
            user_agent="curl/8.5.0",
        )

        email = latest_session_email(db_session, user.email)
        assert email is not None
        assert "curl/8.5.0" in email.body_text
        assert "Unknown device" not in email.body_text

    async def test_no_device_name_and_no_user_agent_still_says_unknown(self, db_session: Session):
        user = make_user(db_session)
        service = EmailService()

        await service.send_security_new_session(
            db_session,
            user.email,
            user.id,
            device_name=None,
            ip_address="203.0.113.5",
            user_agent=None,
        )

        email = latest_session_email(db_session, user.email)
        assert email is not None
        assert "Unknown device" in email.body_text

    async def test_explicit_device_name_still_wins_over_user_agent(self, db_session: Session):
        user = make_user(db_session)
        service = EmailService()

        await service.send_security_new_session(
            db_session,
            user.email,
            user.id,
            device_name="MacBook Pro",
            ip_address="203.0.113.5",
            user_agent="obsidian/1.6.7",
        )

        email = latest_session_email(db_session, user.email)
        assert email is not None
        assert "MacBook Pro" in email.body_text
        assert "obsidian/1.6.7" not in email.body_text
