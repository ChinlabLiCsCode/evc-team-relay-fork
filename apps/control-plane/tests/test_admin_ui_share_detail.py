"""Regression test for #6ede8840: GET /admin-ui/shares/{id} returned HTTP 500
as soon as the share had >=1 member.

Root cause: share_service.list_members() returns plain dicts (its own
docstring says so), but admin_ui.share_detail did `m.user_id` — attribute
access on a dict raises AttributeError. With zero members the set
comprehension body never executes, so the bug was invisible on any share
with no members, which is exactly how it slipped through.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core import security
from app.db import models


def make_admin_user(db: Session, email: str, password: str = "test123456") -> models.User:
    user = models.User(
        email=email,
        password_hash=security.get_password_hash(password),
        is_admin=True,
        is_active=True,
        email_verified=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def make_plain_user(db: Session, email: str, password: str = "test123456") -> models.User:
    user = models.User(
        email=email,
        password_hash=security.get_password_hash(password),
        is_admin=False,
        is_active=True,
        email_verified=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def login_admin_ui(client: TestClient, email: str, password: str = "test123456") -> str:
    response = client.post(
        "/admin-ui/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text
    token = response.cookies.get("admin_token")
    assert token, "admin_token cookie not set on login"
    return token


class TestAdminUiShareDetailWithMembers:
    def test_share_detail_200_with_zero_members(self, db_session: Session, client: TestClient):
        """Baseline: the route already worked with zero members — must keep working."""
        admin = make_admin_user(db_session, "admin-zero-members@example.com")
        token = login_admin_ui(client, admin.email)

        share = models.Share(
            kind=models.ShareKind.FOLDER,
            path="ZeroMembers/",
            visibility=models.ShareVisibility.PRIVATE,
            owner_user_id=admin.id,
            web_published=False,
            web_folder_items=[],
        )
        db_session.add(share)
        db_session.commit()
        db_session.refresh(share)

        response = client.get(
            f"/admin-ui/shares/{share.id}",
            cookies={"admin_token": token},
        )
        assert response.status_code == 200, response.text
        assert "No members yet." in response.text

    def test_share_detail_200_with_one_member(self, db_session: Session, client: TestClient):
        """The actual regression: this 500'd before the fix, on ANY share with >=1 member."""
        admin = make_admin_user(db_session, "admin-with-members@example.com")
        member_user = make_plain_user(db_session, "member@example.com")
        token = login_admin_ui(client, admin.email)

        share = models.Share(
            kind=models.ShareKind.FOLDER,
            path="HasMembers/",
            visibility=models.ShareVisibility.PRIVATE,
            owner_user_id=admin.id,
            web_published=False,
            web_folder_items=[],
        )
        db_session.add(share)
        db_session.commit()
        db_session.refresh(share)

        member = models.ShareMember(
            share_id=share.id, user_id=member_user.id, role=models.ShareMemberRole.EDITOR
        )
        db_session.add(member)
        db_session.commit()

        response = client.get(
            f"/admin-ui/shares/{share.id}",
            cookies={"admin_token": token},
        )
        assert response.status_code == 200, response.text
        assert member_user.email in response.text
        assert "No members yet." not in response.text

    def test_share_detail_200_with_multiple_members(self, db_session: Session, client: TestClient):
        """Not just off-by-one: two members must also render fine."""
        admin = make_admin_user(db_session, "admin-multi-members@example.com")
        member_a = make_plain_user(db_session, "member-a@example.com")
        member_b = make_plain_user(db_session, "member-b@example.com")
        token = login_admin_ui(client, admin.email)

        share = models.Share(
            kind=models.ShareKind.FOLDER,
            path="MultiMembers/",
            visibility=models.ShareVisibility.PRIVATE,
            owner_user_id=admin.id,
            web_published=False,
            web_folder_items=[],
        )
        db_session.add(share)
        db_session.commit()
        db_session.refresh(share)

        db_session.add_all(
            [
                models.ShareMember(
                    share_id=share.id, user_id=member_a.id, role=models.ShareMemberRole.VIEWER
                ),
                models.ShareMember(
                    share_id=share.id, user_id=member_b.id, role=models.ShareMemberRole.EDITOR
                ),
            ]
        )
        db_session.commit()

        response = client.get(
            f"/admin-ui/shares/{share.id}",
            cookies={"admin_token": token},
        )
        assert response.status_code == 200, response.text
        assert member_a.email in response.text
        assert member_b.email in response.text
