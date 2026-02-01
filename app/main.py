"""FastAPI application with all endpoints."""
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Cookie, Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db, init_db, SessionLocal
from .fitbit import fitbit_client
from .influxdb_client import weight_db
from .models import AccessLog, ShareToken
from .scheduler import sync_scheduler

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def import_admin_token():
    """Import admin token from env ONLY if no admin exists in DB."""
    db = SessionLocal()
    try:
        # Check if ANY admin exists
        any_admin = (
            db.query(ShareToken)
            .filter(ShareToken.is_admin == True, ShareToken.is_active == True)
            .first()
        )
        if any_admin:
            logger.info(f"Admin already exists: {any_admin.name} - skipping .env import")
            return

        # No admin exists - import from .env
        token = ShareToken(
            token=settings.admin_token,
            name="Admin",
            is_admin=True,
        )
        db.add(token)
        db.commit()
        logger.info(f"Imported admin token from .env (first-time setup)")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    logger.info("Starting Fitbit Weight Tracker...")
    init_db()
    import_admin_token()
    sync_scheduler.start()

    if fitbit_client.is_authenticated():
        logger.info("Running initial sync...")
        await sync_scheduler.run_now()

    yield

    sync_scheduler.stop()
    logger.info("Shutting down...")


app = FastAPI(
    title="Fitbit Weight Tracker",
    description="Self-hosted weight tracking with Fitbit sync",
    lifespan=lifespan,
)

templates = Jinja2Templates(directory="app/templates")

try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
except RuntimeError:
    pass


# ============================================
# Helper: Token validation
# ============================================


def get_token_from_request(
    request: Request,
    token: str | None = Query(None),
    db: Session = Depends(get_db),
) -> ShareToken | None:
    """Get and validate token from query param or cookie."""
    # Try query param first
    token_str = token

    # Try cookie if no query param
    if not token_str:
        token_str = request.cookies.get("token")

    if not token_str:
        return None

    share_token = (
        db.query(ShareToken)
        .filter(ShareToken.token == token_str, ShareToken.is_active == True)
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


# ============================================
# Public: Weight View
# ============================================


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    token: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """Main page with weight graph."""
    share_token = get_token_from_request(request, token, db)

    if not share_token:
        return templates.TemplateResponse("login.html", {"request": request})

    response = templates.TemplateResponse(
        "index.html",
        {"request": request, "token": share_token.token, "is_admin": share_token.is_admin},
    )
    # Set cookie for future visits
    response.set_cookie("token", share_token.token, httponly=True, secure=settings.secure_cookies, samesite="lax", max_age=60*60*24*365)
    return response


@app.get("/api/weight")
async def get_weight(
    request: Request,
    days: int = Query(default=30, ge=1, le=9999),
    token: str | None = Query(None),
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
    db: Session = Depends(get_db),
):
    """Get weight history as JSON."""
    share_token = get_token_from_request(request, token, db)
    if not share_token:
        raise HTTPException(status_code=401, detail="Token required")

    if from_date and to_date:
        history = weight_db.get_weight_range(from_date, to_date)
    else:
        history = weight_db.get_weight_history(days)
    return {"weight": history, "days": days}


@app.get("/api/goal")
async def get_goal(
    request: Request,
    token: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """Get weight goal from Fitbit."""
    share_token = get_token_from_request(request, token, db)
    if not share_token:
        raise HTTPException(status_code=401, detail="Token required")

    if not fitbit_client.is_authenticated():
        raise HTTPException(status_code=503, detail="Fitbit not connected")

    goal = await fitbit_client.get_weight_goal()
    return {"goal": goal}


@app.get("/api/stats")
async def get_stats(
    request: Request,
    days: int = Query(default=30, ge=1, le=9999),
    token: str | None = Query(None),
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
    db: Session = Depends(get_db),
):
    """Get weight statistics."""
    share_token = get_token_from_request(request, token, db)
    if not share_token:
        raise HTTPException(status_code=401, detail="Token required")

    if from_date and to_date:
        stats = weight_db.get_stats_range(from_date, to_date)
    else:
        stats = weight_db.get_stats(days)
    return stats


# ============================================
# Auth: Login/Logout
# ============================================


@app.post("/login")
async def login_post(
    request: Request,
    token: str = Form(...),
    db: Session = Depends(get_db),
):
    """Handle login form submission."""
    share_token = (
        db.query(ShareToken)
        .filter(ShareToken.token == token, ShareToken.is_active == True)
        .first()
    )

    if not share_token:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Ungueltiger Token"},
        )

    response = RedirectResponse("/", status_code=303)
    response.set_cookie("token", token, httponly=True, secure=settings.secure_cookies, samesite="lax", max_age=60*60*24*365)
    return response


@app.get("/logout")
async def logout():
    """Clear token cookie."""
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("token")
    return response


# ============================================
# Fitbit OAuth
# ============================================


@app.get("/fitbit/login")
async def fitbit_login():
    """Start Fitbit OAuth flow."""
    auth_url = fitbit_client.get_auth_url()
    return RedirectResponse(auth_url)


@app.get("/callback")
async def callback(code: str | None = None, error: str | None = None):
    """Handle Fitbit OAuth callback."""
    if error:
        return HTMLResponse(f"<h1>Error: {error}</h1>", status_code=400)

    if not code:
        return HTMLResponse("<h1>No code received</h1>", status_code=400)

    try:
        await fitbit_client.exchange_code(code)
        logger.info("Fitbit authentication successful")
        await sync_scheduler.run_now()

        return HTMLResponse("""
            <!DOCTYPE html>
            <html><head><title>Erfolg!</title>
            <style>body{font-family:sans-serif;background:#0f172a;color:#eee;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;}</style>
            </head><body><div style="text-align:center">
                <h1>Fitbit verbunden!</h1>
                <p>Die Gewichtsdaten werden synchronisiert.</p>
                <p><a href="/admin" style="color:#3b82f6">Zurueck zum Admin</a></p>
            </div></body></html>
        """)
    except Exception as e:
        logger.error(f"OAuth callback failed: {e}")
        return HTMLResponse(f"<h1>Authentication failed</h1><pre>{e}</pre>", status_code=500)


# ============================================
# Admin GUI
# ============================================


def require_admin(
    request: Request,
    token: str | None = Query(None),
    db: Session = Depends(get_db),
) -> ShareToken:
    """Require admin token for access."""
    share_token = get_token_from_request(request, token, db)
    if not share_token or not share_token.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return share_token


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(
    request: Request,
    db: Session = Depends(get_db),
):
    """Admin dashboard."""
    share_token = get_token_from_request(request, None, db)
    if not share_token or not share_token.is_admin:
        return RedirectResponse("/?error=admin_required", status_code=303)

    tokens = db.query(ShareToken).order_by(ShareToken.created_at.desc()).all()

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "tokens": tokens,
            "current_token_id": share_token.id,
            "fitbit_connected": fitbit_client.is_authenticated(),
            "sync_interval": settings.sync_interval_hours,
        },
    )


@app.post("/admin/tokens/create")
async def create_token(
    request: Request,
    name: str = Form(...),
    is_admin: bool = Form(False),
    db: Session = Depends(get_db),
):
    """Create a new token."""
    share_token = get_token_from_request(request, None, db)
    if not share_token or not share_token.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    new_token = ShareToken(
        token=str(uuid.uuid4()),
        name=name,
        is_admin=is_admin,
    )
    db.add(new_token)
    db.commit()

    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/tokens/{token_id}/revoke")
async def revoke_token(
    request: Request,
    token_id: int,
    db: Session = Depends(get_db),
):
    """Revoke a token."""
    share_token = get_token_from_request(request, None, db)
    if not share_token or not share_token.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    token = db.query(ShareToken).filter(ShareToken.id == token_id).first()
    if token:
        token.is_active = False
        db.commit()

    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/tokens/{token_id}/activate")
async def activate_token(
    request: Request,
    token_id: int,
    db: Session = Depends(get_db),
):
    """Re-activate a token."""
    share_token = get_token_from_request(request, None, db)
    if not share_token or not share_token.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    token = db.query(ShareToken).filter(ShareToken.id == token_id).first()
    if token:
        token.is_active = True
        db.commit()

    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/tokens/{token_id}/regenerate")
async def regenerate_token(
    request: Request,
    token_id: int,
    db: Session = Depends(get_db),
):
    """Regenerate a token - creates new random token value."""
    share_token = get_token_from_request(request, None, db)
    if not share_token or not share_token.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    token = db.query(ShareToken).filter(ShareToken.id == token_id).first()
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")

    # Generate new token
    new_token_value = str(uuid.uuid4())
    token.token = new_token_value
    db.commit()

    # If regenerating own token, need to update cookie
    is_own_token = token_id == share_token.id

    # Show the new token (one-time display)
    response = templates.TemplateResponse(
        "token_regenerated.html",
        {
            "request": request,
            "token": token,
            "new_token": new_token_value,
            "is_own_token": is_own_token,
        },
    )

    # Update cookie if own token
    if is_own_token:
        response.set_cookie("token", new_token_value, httponly=True, secure=settings.secure_cookies, samesite="lax", max_age=60*60*24*365)

    return response


@app.get("/admin/tokens/{token_id}/logs", response_class=HTMLResponse)
async def token_logs(
    request: Request,
    token_id: int,
    db: Session = Depends(get_db),
):
    """View access logs for a token."""
    share_token = get_token_from_request(request, None, db)
    if not share_token or not share_token.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    token = db.query(ShareToken).filter(ShareToken.id == token_id).first()
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")

    logs = (
        db.query(AccessLog)
        .filter(AccessLog.token_id == token_id)
        .order_by(AccessLog.accessed_at.desc())
        .limit(100)
        .all()
    )

    return templates.TemplateResponse(
        "logs.html",
        {"request": request, "token": token, "logs": logs},
    )


@app.post("/admin/sync")
async def trigger_sync(
    request: Request,
    db: Session = Depends(get_db),
):
    """Manually trigger full Fitbit sync (all data)."""
    share_token = get_token_from_request(request, None, db)
    if not share_token or not share_token.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    if not fitbit_client.is_authenticated():
        raise HTTPException(status_code=503, detail="Fitbit not connected")

    # Sync all data (20 years should cover everything)
    await sync_scheduler.run_full_sync(days=20*365)
    return RedirectResponse("/admin", status_code=303)
