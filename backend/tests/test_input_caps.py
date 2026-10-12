"""Input length/item caps."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from vibony_backend.schemas import InviteCreateRequest, LoginRequest, SignupRequest
from vibony_backend.tasks.schemas import MAX_JSONB_BYTES, StarterFile, TaskCreate


# ---------------------------------------------------------------------------
# Password length cap — argon2 DoS guard
# ---------------------------------------------------------------------------

def test_password_length_capped():
    long = "a" * 200
    with pytest.raises(ValidationError):
        SignupRequest(email="a@b.co", password=long, company_name="W")
    with pytest.raises(ValidationError):
        LoginRequest(email="a@b.co", password=long)
    # 128 is allowed
    SignupRequest(email="a@b.co", password="a" * 128, company_name="W")


def test_company_name_capped():
    with pytest.raises(ValidationError):
        SignupRequest(email="a@b.co", password="goodpassword", company_name="x" * 300)


# ---------------------------------------------------------------------------
# Invite email is validated
# ---------------------------------------------------------------------------

def test_invite_email_must_be_valid():
    with pytest.raises(ValidationError):
        InviteCreateRequest(role="interviewer", email="not-an-email")
    # None (open invite) and a valid email are both fine
    InviteCreateRequest(role="interviewer", email=None)
    InviteCreateRequest(role="interviewer", email="a@b.co")


# ---------------------------------------------------------------------------
# Task authoring caps
# ---------------------------------------------------------------------------

def _task(**over):
    base = dict(slug="t1", title="T", runtime_image="vibony/sandbox-py:3.12")
    base.update(over)
    return base


def test_task_title_capped():
    with pytest.raises(ValidationError):
        TaskCreate(**_task(title="x" * 300))


def test_task_too_many_starter_files():
    files = [{"path": f"f{i}.py", "content": ""} for i in range(101)]
    with pytest.raises(ValidationError):
        TaskCreate(**_task(starter_files=files))


def test_task_starter_file_content_capped():
    with pytest.raises(ValidationError):
        StarterFile(path="f.py", content="x" * (256 * 1024 + 1))


def test_task_jsonb_size_capped():
    big = {"k": "x" * (MAX_JSONB_BYTES + 10)}
    with pytest.raises(ValidationError):
        TaskCreate(**_task(weakness=big))
