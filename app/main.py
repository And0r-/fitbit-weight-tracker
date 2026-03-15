"""FastAPI application with all endpoints."""
import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Cookie, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db, init_db, SessionLocal
from .fitbit import fitbit_client
from .food import save_uploaded_photo
from .food_queue import get_queue_status, retry_failed_jobs, schedule_analysis
from .influxdb_client import weight_db
from .models import AccessLog, Meal, MealPhoto, ShareToken
from .oura import oura_client
from .scheduler import sync_scheduler
from .summary import build_health_summary

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

# Serve food photos
from pathlib import Path
food_dir = Path("/app/data/food")
food_dir.mkdir(parents=True, exist_ok=True)
(food_dir / "thumbs").mkdir(exist_ok=True)
try:
    app.mount("/food", StaticFiles(directory=str(food_dir)), name="food")
except RuntimeError:
    pass


@app.get("/favicon.ico")
async def favicon():
    return RedirectResponse("/static/favicon-32x32.ico", status_code=301)


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
        {
            "request": request,
            "token": share_token.token,
            "is_admin": share_token.is_admin,
            "can_view_oura": share_token.can_view_oura,
            "can_view_food": share_token.can_view_food,
            "default_period": settings.default_period,
        },
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
            "oura_connected": oura_client.is_authenticated(),
            "sync_interval": settings.sync_interval_minutes,
            "queue_status": get_queue_status(db),
        },
    )


@app.post("/admin/tokens/create")
async def create_token(
    request: Request,
    name: str = Form(...),
    is_admin: bool = Form(False),
    can_view_oura: bool = Form(False),
    can_view_food: bool = Form(False),
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
        can_view_oura=can_view_oura,
        can_view_food=can_view_food,
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


@app.post("/admin/tokens/{token_id}/toggle-oura")
async def toggle_oura(
    request: Request,
    token_id: int,
    db: Session = Depends(get_db),
):
    """Toggle Oura access for a token."""
    share_token = get_token_from_request(request, None, db)
    if not share_token or not share_token.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    token = db.query(ShareToken).filter(ShareToken.id == token_id).first()
    if token:
        token.can_view_oura = not token.can_view_oura
        db.commit()

    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/tokens/{token_id}/toggle-food")
async def toggle_food(
    request: Request,
    token_id: int,
    db: Session = Depends(get_db),
):
    """Toggle food access for a token."""
    share_token = get_token_from_request(request, None, db)
    if not share_token or not share_token.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    token = db.query(ShareToken).filter(ShareToken.id == token_id).first()
    if token:
        token.can_view_food = not token.can_view_food
        db.commit()

    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/tokens/{token_id}/toggle-admin")
async def toggle_admin(
    request: Request,
    token_id: int,
    db: Session = Depends(get_db),
):
    """Toggle admin status for a token."""
    share_token = get_token_from_request(request, None, db)
    if not share_token or not share_token.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    token = db.query(ShareToken).filter(ShareToken.id == token_id).first()
    if token and token.id != share_token.id:  # Can't remove own admin
        token.is_admin = not token.is_admin
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


# ============================================
# Oura OAuth
# ============================================


@app.get("/oura/login")
async def oura_login(
    request: Request,
    db: Session = Depends(get_db),
):
    """Start Oura OAuth flow (admin only)."""
    share_token = get_token_from_request(request, None, db)
    if not share_token or not share_token.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    auth_url = oura_client.get_auth_url()
    return RedirectResponse(auth_url)


@app.get("/oura/callback")
async def oura_callback(code: str | None = None, error: str | None = None):
    """Handle Oura OAuth callback."""
    if error:
        return HTMLResponse(f"<h1>Error: {error}</h1>", status_code=400)

    if not code:
        return HTMLResponse("<h1>No code received</h1>", status_code=400)

    try:
        await oura_client.exchange_code(code)
        logger.info("Oura authentication successful")
        await sync_scheduler.run_oura_now()

        return HTMLResponse("""
            <!DOCTYPE html>
            <html><head><title>Erfolg!</title>
            <style>body{font-family:sans-serif;background:#0f172a;color:#eee;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;}</style>
            </head><body><div style="text-align:center">
                <h1>Oura Ring verbunden!</h1>
                <p>Die Daten werden synchronisiert.</p>
                <p><a href="/admin" style="color:#3b82f6">Zurueck zum Admin</a></p>
            </div></body></html>
        """)
    except Exception as e:
        logger.error(f"Oura OAuth callback failed: {e}")
        return HTMLResponse(f"<h1>Authentication failed</h1><pre>{e}</pre>", status_code=500)


@app.post("/admin/oura/sync")
async def trigger_oura_sync(
    request: Request,
    db: Session = Depends(get_db),
):
    """Manually trigger full Oura sync."""
    share_token = get_token_from_request(request, None, db)
    if not share_token or not share_token.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    if not oura_client.is_authenticated():
        raise HTTPException(status_code=503, detail="Oura not connected")

    await sync_scheduler.run_oura_full(days=30)
    return RedirectResponse("/admin", status_code=303)


# ============================================
# Oura API Endpoints
# ============================================


def _require_oura(share_token: ShareToken | None):
    """Check token has Oura access."""
    if not share_token:
        raise HTTPException(status_code=401, detail="Token required")
    if not share_token.can_view_oura:
        raise HTTPException(status_code=403, detail="Oura access not granted")


@app.get("/api/oura/sleep")
async def get_oura_sleep(
    request: Request,
    days: int = Query(default=3, ge=1, le=30),
    token: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """Get Oura sleep data."""
    share_token = get_token_from_request(request, token, db)
    _require_oura(share_token)
    return weight_db.get_sleep_history(days)


@app.get("/api/oura/readiness")
async def get_oura_readiness(
    request: Request,
    days: int = Query(default=3, ge=1, le=30),
    token: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """Get Oura readiness data."""
    share_token = get_token_from_request(request, token, db)
    _require_oura(share_token)
    return weight_db.get_readiness_history(days)


@app.get("/api/oura/heartrate")
async def get_oura_heartrate(
    request: Request,
    hours: int = Query(default=24, ge=1, le=168),
    token: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """Get Oura heart rate data."""
    share_token = get_token_from_request(request, token, db)
    _require_oura(share_token)
    return weight_db.get_heart_rate_history(hours)


@app.get("/api/oura/stress")
async def get_oura_stress(
    request: Request,
    days: int = Query(default=1, ge=1, le=30),
    token: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """Get Oura stress data."""
    share_token = get_token_from_request(request, token, db)
    _require_oura(share_token)
    return weight_db.get_stress_history(days)


# ============================================
# Health Summary (for AI assistants)
# ============================================


@app.get("/api/summary")
async def get_health_summary(
    request: Request,
    token: str | None = Query(None),
    sync: bool = Query(default=True),
    db: Session = Depends(get_db),
):
    """Compact health summary for AI consumption.

    Triggers a fresh sync of weight + Oura data before returning.
    Set ?sync=false to skip the sync and return cached data.
    """
    share_token = get_token_from_request(request, token, db)
    _require_oura(share_token)

    # Trigger fresh sync (parallel)
    if sync:
        tasks = []
        if fitbit_client.is_authenticated():
            tasks.append(sync_scheduler.run_now(days=7))
        if oura_client.is_authenticated():
            tasks.append(sync_scheduler.run_oura_now(days=3))
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    logger.error(f"Summary sync failed: {r}")

    # Get goal weight from Fitbit
    goal = None
    if fitbit_client.is_authenticated():
        try:
            goal_data = await fitbit_client.get_weight_goal()
            if goal_data:
                goal = goal_data.get("weight")
        except Exception:
            pass

    return await build_health_summary(goal_weight=goal)


# ============================================
# Food Tracking
# ============================================


def _require_food(share_token: ShareToken | None):
    """Check token has food access."""
    if not share_token:
        raise HTTPException(status_code=401, detail="Token required")
    if not share_token.can_view_food and not share_token.is_admin:
        raise HTTPException(status_code=403, detail="Food access not granted")


@app.post("/api/food/upload")
async def upload_food_photos(
    request: Request,
    files: list[UploadFile] = File(...),
    token: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """Upload food photos. Only admins can upload."""
    share_token = get_token_from_request(request, token, db)
    if not share_token or not share_token.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    results = []
    meals_to_analyze = set()

    for file in files:
        if not file.content_type or not file.content_type.startswith("image/"):
            continue

        data = await file.read()
        if len(data) > 10 * 1024 * 1024:  # 10MB limit
            continue

        meal, photo = save_uploaded_photo(db, data, file.filename or "upload.jpg")
        meals_to_analyze.add(meal.id)
        results.append({
            "photo_id": photo.id,
            "meal_id": meal.id,
            "meal_day": meal.day,
            "photo_taken_at": photo.photo_taken_at.isoformat(),
        })

    # Schedule analysis for affected meals (with debounce)
    for meal_id in meals_to_analyze:
        meal = db.query(Meal).filter(Meal.id == meal_id).first()
        if meal:
            schedule_analysis(db, meal)

    return {"uploaded": len(results), "photos": results}


@app.get("/api/food")
async def get_food_gallery(
    request: Request,
    days: int = Query(default=7, ge=1, le=90),
    show_cheat: bool = Query(default=False),
    token: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """Get food gallery data."""
    share_token = get_token_from_request(request, token, db)
    _require_food(share_token)

    from .food import compute_food_day
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(settings.timezone)
    cutoff_day = (datetime.now(tz) - timedelta(days=days)).strftime("%Y-%m-%d")

    query = db.query(Meal).filter(Meal.day >= cutoff_day)
    if not show_cheat:
        query = query.filter(Meal.is_cheat_day == False)

    meals = query.order_by(Meal.first_photo_at.desc()).all()

    result = []
    for meal in meals:
        photos = [{
            "id": p.id,
            "thumbnail": f"/food/thumbs/{p.thumbnail_path.split('/')[-1]}" if p.thumbnail_path else None,
            "full": f"/food/{p.filename}",
            "taken_at": p.photo_taken_at.isoformat(),
            "type": p.photo_type,
        } for p in meal.photos]

        result.append({
            "id": meal.id,
            "day": meal.day,
            "time": meal.first_photo_at.isoformat(),
            "is_cheat_day": meal.is_cheat_day,
            "status": meal.analysis_status,
            "photos": photos,
            "health_score": meal.health_score,
            "health_color": meal.health_color,
            "total_calories": meal.total_calories,
            "total_protein_g": meal.total_protein_g,
            "total_carbs_g": meal.total_carbs_g,
            "total_fat_g": meal.total_fat_g,
            "items": meal.items_json,
            "ai_comment": meal.ai_comment,
        })

    return {"meals": result, "days": days, "show_cheat": show_cheat}


@app.post("/admin/food/retry")
async def retry_food_analysis(
    request: Request,
    db: Session = Depends(get_db),
):
    """Retry all failed food analyses."""
    share_token = get_token_from_request(request, None, db)
    if not share_token or not share_token.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    count = retry_failed_jobs(db)
    return RedirectResponse("/admin", status_code=303)
