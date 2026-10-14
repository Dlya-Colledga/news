import hashlib
import secrets
from datetime import timedelta
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
from fastapi import HTTPException
from sqlalchemy import select
from app.db.models import User, LoginAttempt, utcnow
from app.services.deduplication import aware

hasher = PasswordHasher()
DUMMY_HASH = hasher.hash("not-a-user-password-" + secrets.token_urlsafe(16))


def csrf_token(request):
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(32)
    return request.session["csrf"]


def verify_csrf(request, token):
    expected = request.session.get("csrf", "")
    if not expected or not token or not secrets.compare_digest(expected, str(token)):
        raise HTTPException(403, "Invalid CSRF token")


def current_user(request, db):
    user_id = request.session.get("user_id")
    user = db.get(User, user_id) if isinstance(user_id, int) else None
    if not user or not user.active or user.session_version != request.session.get("version"):
        return None
    return user


def require_user(request, db, admin=False):
    user = current_user(request, db)
    if not user:
        raise HTTPException(303, headers={"Location": "/admin/login"})
    if admin and user.role != "admin":
        raise HTTPException(403, "Administrator role required")
    return user


def authenticate(db, username, password, ip):
    key = hashlib.sha256((ip + ":" + username.casefold()).encode()).hexdigest()
    # Upsert creates a shared rate-limit row even when concurrent attempts start together.
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert

        db.execute(
            insert(LoginAttempt)
            .values(key=key, failures=0, window_started_at=utcnow())
            .on_conflict_do_nothing()
        )
    attempt = db.scalar(select(LoginAttempt).where(LoginAttempt.key == key).with_for_update())
    if not attempt:
        attempt = LoginAttempt(key=key, failures=0, window_started_at=utcnow())
        db.add(attempt)
    if aware(attempt.window_started_at) < utcnow() - timedelta(minutes=15):
        attempt.failures, attempt.window_started_at = 0, utcnow()
    if attempt.failures >= 8:
        db.commit()
        raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")
    user = db.scalar(select(User).where(User.username == username))
    valid = False
    try:
        valid = hasher.verify(user.password_hash if user else DUMMY_HASH, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        pass
    if not user or not user.active or not valid:
        attempt.failures += 1
        db.commit()
        return None
    attempt.failures = 0
    if hasher.check_needs_rehash(user.password_hash):
        user.password_hash = hasher.hash(password)
    db.commit()
    return user
