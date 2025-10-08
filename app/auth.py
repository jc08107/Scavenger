"""
Authentication utilities and dependencies for the scavenger hunt app.

This module provides functions to hash and verify passwords using passlib's
bcrypt scheme, as well as FastAPI dependencies for retrieving the current
authenticated user from the session.  It also includes helper functions to
enforce user roles on protected routes.

Note: The passlib dependency must be installed in the environment where this
application is run.  The `requirements.txt` file includes passlib[bcrypt].
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from passlib.context import CryptContext

from .database import get_db
from .models import User


# Password hashing context using bcrypt.  Bcrypt is computationally expensive
# and designed to slow down brute force attacks.
pwd_context = CryptContext(
    schemes=["pbkdf2_sha256"],
    deprecated="auto",
)



def get_password_hash(password: str) -> str:
    """Hash a plaintext password using bcrypt."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against a hashed password."""
    return pwd_context.verify(plain_password, hashed_password)


def get_current_user(
    request: Request, db: Session = Depends(get_db)
) -> User:
    """Retrieve the currently authenticated user based on session cookie.

    Raises HTTPException(401) if the user is not authenticated or no
    corresponding user record is found.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        # Session may be stale or invalid; clear it
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def require_role(required_role: str):
    """Factory to create a dependency that ensures the current user has a specific role.

    Usage::

        @app.get("/protected")
        def protected_route(user: User = Depends(require_role("admin"))):
            ...
    """

    def role_dependency(user: User = Depends(get_current_user)) -> User:
        if user.role != required_role:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires {required_role} role",
            )
        return user

    return role_dependency