from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from jose import JWTError, jwt
from passlib.context import CryptContext

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 14

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def jwt_secret() -> str:
    s = os.getenv("JWT_SECRET", "").strip()
    if not s or len(s) < 16:
        raise RuntimeError(
            "JWT_SECRET manquant ou trop court (min. 16 caractères). "
            "Définis JWT_SECRET dans le fichier .env du backend."
        )
    return s


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(user_id: int, email: str) -> str:
    now = datetime.now(timezone.utc)
    expire = now + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "email": email,
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
    }
    return jwt.encode(payload, jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        return jwt.decode(token, jwt_secret(), algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None
