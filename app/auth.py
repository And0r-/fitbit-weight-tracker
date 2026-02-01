"""Authentication and authorization for share tokens and admin access."""
from datetime import datetime

from fastapi import Depends, Header, HTTPException, Query, Request
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .models import AccessLog, ShareToken


def get_share_token(
    request: Request,
    token: str | None = Query(None),
    db: Session = Depends(get_db),
) -> ShareToken:
    """Validate share token from query parameter.

    Usage: Add as dependency to protected endpoints.
    Token can be passed as ?token=xxx query parameter.
    """
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Token required. Add ?token=your-token to the URL.",
        )

    share_token = (
        db.query(ShareToken)
        .filter(ShareToken.token == token, ShareToken.is_active == True)
        .first()
    )

    if not share_token:
        raise HTTPException(status_code=401, detail="Invalid or inactive token")

    # Log access
    log = AccessLog(
        token_id=share_token.id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    db.add(log)

    # Update last used
    share_token.last_used_at = datetime.utcnow()
    db.commit()

    return share_token


def require_admin(x_admin_token: str = Header(...)) -> bool:
    """Verify admin token from header.

    Usage: Add as dependency to admin endpoints.
    Requires X-Admin-Token header.
    """
    if x_admin_token != settings.admin_token:
        raise HTTPException(status_code=403, detail="Invalid admin token")
    return True


def optional_share_token(
    request: Request,
    token: str | None = Query(None),
    db: Session = Depends(get_db),
) -> ShareToken | None:
    """Optional token validation - returns None if not provided.

    Useful for pages that work both with and without auth.
    """
    if not token:
        return None

    share_token = (
        db.query(ShareToken)
        .filter(ShareToken.token == token, ShareToken.is_active == True)
        .first()
    )

    if share_token:
        # Log access
        log = AccessLog(
            token_id=share_token.id,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        db.add(log)
        share_token.last_used_at = datetime.utcnow()
        db.commit()

    return share_token
