import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from lockstep.config import get_settings

settings = get_settings()

# bcrypt's own limit — used directly (not via passlib, which is unmaintained
# and incompatible with bcrypt >=4.1's stricter length check).
_MAX_PASSWORD_BYTES = 72


def hash_password(password: str) -> str:
    truncated = password.encode("utf-8")[:_MAX_PASSWORD_BYTES]
    return bcrypt.hashpw(truncated, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed_password: str) -> bool:
    truncated = password.encode("utf-8")[:_MAX_PASSWORD_BYTES]
    return bcrypt.checkpw(truncated, hashed_password.encode("utf-8"))


def create_access_token(user_id: uuid.UUID) -> str:
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": str(user_id), "exp": expires_at}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> uuid.UUID:
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    return uuid.UUID(payload["sub"])
