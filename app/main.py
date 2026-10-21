import asyncio
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app import api, api_master, api_users, api_write
from app.config import settings
from app.db import SessionLocal, engine, init_db, wait_for_db
from app.drivers import zte
from app.models import User
from app.security import NotAuthenticated, authenticate_user, login_blocked, login_failed
from app.services.runtime import run_forever

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await wait_for_db()
    await init_db()
    task = asyncio.create_task(run_forever()) if settings.run_workers else None
    yield
    if task:
        task.cancel()
    await zte.links.close_all()


app = FastAPI(title="ControlOLT Manager", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
app.include_router(api.router)
app.include_router(api_master.router)
app.include_router(api_write.router)
app.include_router(api_users.router)
app.include_router(api.ws_router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


READ_ONLY_PAGES = {"/", "/configured", "/diagnostics"}
READ_ONLY_API = (
    re.compile(r"/api/(?:summary|olts|filters|onus|onus-export|onu-types)$"),
    re.compile(r"/api/olts/\d+$"),
    re.compile(r"/api/onus/\d+(?:/(?:status|running-config|sw-info|traffic|eth))?$"),
    re.compile(r"/api/olts/\d+/diag$"),
)


@app.middleware("http")
async def authorize_role(request: Request, call_next):
    path = request.url.path
    if path in ("/login", "/logout", "/health") or path.startswith("/static/"):
        return await call_next(request)
    user_id = request.session.get("user_id")
    user = None
    if user_id:
        async with SessionLocal() as session:
            user = await session.get(User, user_id)
    if not user:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Not logged in"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    request.state.user = user
    request.state.user_role = user.role
    if user.role == "read_only":
        page_allowed = request.method == "GET" and (path in READ_ONLY_PAGES or re.fullmatch(r"/onus/\d+", path))
        api_allowed = request.method == "GET" and any(pattern.fullmatch(path) for pattern in READ_ONLY_API)
        if not (page_allowed or api_allowed) and not path.startswith("/static/"):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Read-only accounts can access Configured and Diagnostics only."}, status_code=403)
            return HTMLResponse("Read-only accounts can access Configured and Diagnostics only.", status_code=403)
    return await call_next(request)


app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="lax",
                   https_only=settings.https_only, max_age=12 * 3600)


@app.exception_handler(NotAuthenticated)
async def _not_auth(request: Request, _):
    if request.url.path.startswith("/api"):
        return JSONResponse({"detail": "Not logged in"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    ip = request.client.host if request.client else "?"
    if login_blocked(ip):
        return templates.TemplateResponse(request, "login.html",
                                          {"error": "Too many login attempts. Try again in a few minutes."}, status_code=429)
    user = await authenticate_user(username, password)
    if user:
        request.session.clear()
        request.session["user"] = user.username
        request.session["user_id"] = user.id
        return RedirectResponse("/", status_code=303)
    login_failed(ip)
    return templates.TemplateResponse(request, "login.html", {"error": "Incorrect username or password"}, status_code=401)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def _page(request: Request, name: str, **ctx):
    user = getattr(request.state, "user", None)
    if not user:
        raise NotAuthenticated()
    return templates.TemplateResponse(request, name, {
        "user": user.username, "user_role": user.role, **ctx,
    })


@app.get("/")
async def home():
    return RedirectResponse("/configured", status_code=303)


for _path, _tpl, _active in [("/configured", "configured.html", "configured"), ("/diagnostics", "diagnostics.html", "diagnostics"),
                             ("/unconfigured", "unconfigured.html", "unconfigured"), ("/graphs", "graphs.html", "graphs"),
                             ("/settings/olts", "settings_olts.html", "settings"),
                             ("/settings/users", "users.html", "settings"),
                             ("/settings/zones", "crud.html", "settings"), ("/settings/audit", "audit.html", "settings"), ("/settings/speed-profiles", "crud.html", "settings"),
                             ("/settings/onu-types", "crud.html", "settings")]:
    def _make(tpl=_tpl, active=_active, path=_path):
        async def view(request: Request):
            return _page(request, tpl, active=active, crud=path.rsplit("/", 1)[-1])
        return view
    app.add_api_route(_path, _make(), methods=["GET"], response_class=HTMLResponse, include_in_schema=False)


@app.get("/onus", include_in_schema=False)
async def onus_old():
    return RedirectResponse("/configured", status_code=303)


@app.get("/olts/{olt_id}", response_class=HTMLResponse)
async def olt_page(request: Request, olt_id: int):
    return _page(request, "olt.html", olt_id=olt_id, active="settings")


@app.get("/onus/{onu_id}", response_class=HTMLResponse)
async def onu_page(request: Request, onu_id: int):
    return _page(request, "onu.html", onu_id=onu_id, active="configured")


@app.get("/health")
async def health():
    try:
        async with engine.connect() as c:
            await c.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=503)
