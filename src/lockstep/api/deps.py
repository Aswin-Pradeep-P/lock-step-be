import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from lockstep.core.security import decode_access_token
from lockstep.database import get_db
from lockstep.models.user import User
from lockstep.repositories import user_repository

_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


async def get_current_user(
    token: str = Depends(_oauth2_scheme), db: AsyncSession = Depends(get_db)
) -> User:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        user_id = decode_access_token(token)
    except jwt.PyJWTError:
        raise credentials_error from None

    user = await user_repository.get_by_id(db, user_id)
    if user is None:
        raise credentials_error
    return user
