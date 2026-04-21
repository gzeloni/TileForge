"""Shared FastAPI dependencies."""

import os

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.services.jobs import job_service

bearer = HTTPBearer(auto_error=False)


def jobs(request: Request):
    """Return the configured job service."""

    current = getattr(request.app.state, "jobs", None)
    if current is None:
        current = job_service()
        request.app.state.jobs = current
    return current


def require_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> None:
    """Validate the API key from the Authorization header."""

    expected = getattr(request.app.state, "api_key", None) or os.getenv("API_KEY")
    token = credentials.credentials if credentials else None
    if not expected or token != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid api key",
        )
