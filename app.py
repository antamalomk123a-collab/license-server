"""Protected premium-plugin licensing API.
Set ADMIN_KEY and JWT_SECRET to separate, long random values before deployment.
"""
import base64
import hashlib
import hmac
import os
import re
import secrets
import time
import zlib
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Boolean, Column, DateTime, Integer, JSON, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from passlib.hash import bcrypt

DB_URL = os.environ.get("DATABASE_URL", "sqlite:///./licenses.db")
JWT_SECRET = os.environ.get("JWT_SECRET", "")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
SESSION_HOURS = int(os.environ.get("SESSION_HOURS", "6"))
PREMIUM_FOR_ALL = os.environ.get("PREMIUM_FOR_ALL", "true").lower() == "true"
# Optional comma-separated Railway/public host names. Empty permits all hosts.
ALLOWED_HOSTS = {h.strip().lower() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()}

if len(ADMIN_KEY) < 32 or len(JWT_SECRET) < 32 or ADMIN_KEY == JWT_SECRET:
    raise RuntimeError("Use different ADMIN_KEY and JWT_SECRET values of at least 32 characters.")
if not 1 <= SESSION_HOURS <= 24 * 7:
    raise RuntimeError("SESSION_HOURS must be between 1 and 168.")

BASE_DIR = Path(__file__).resolve().parent
PLUGINS_DIR = BASE_DIR / "plugins"
PLUGIN_CATALOG = {
    "autotrade": {"name": "AutoTrade", "filename": "AutoTrade.py", "feature": "premium", "version": "2.0"},
    "autoplevel": {"name": "AutoPLevel", "filename": "AutoPLevel.py", "feature": "premium", "version": "1.0.0"},
}

connect_args = {"check_same_thread": False} if DB_URL.startswith("sqlite") else {}
engine = create_engine(DB_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    hwid = Column(String(128), nullable=True)
    blocked = Column(Boolean, default=False, nullable=False)
    subscription_end = Column(DateTime, nullable=True)
    features = Column(JSON, default=dict, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

Base.metadata.create_all(engine)
app = FastAPI(title="Premium Plugin License Server", docs_url=None, redoc_url=None, openapi_url=None)

# Lightweight abuse protection. For multi-instance deployments put this behind a WAF/rate limiter too.
_attempts = defaultdict(deque)
def _client_ip(request: Request) -> str:
    # Railway sets this header; use the first address only.
    return (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown"))
def _rate_limit(bucket: str, key: str, limit: int, seconds: int):
    now = time.monotonic(); q = _attempts[(bucket, key)]
    while q and q[0] <= now - seconds: q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, "too_many_requests", headers={"Retry-After": str(seconds)})
    q.append(now)

@app.middleware("http")
async def harden(request: Request, call_next):
    host = request.headers.get("host", "").split(":")[0].lower()
    if ALLOWED_HOSTS and host not in ALLOWED_HOSTS:
        return JSONResponse({"detail": "invalid_host"}, status_code=400)
    if request.method not in {"GET", "POST", "DELETE", "OPTIONS"}:
        return JSONResponse({"detail": "method_not_allowed"}, status_code=405)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    return response

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

def require_admin(request: Request, x_admin_key: str = Header("")):
    _rate_limit("admin", _client_ip(request), 30, 60)
    if not x_admin_key or not hmac.compare_digest(x_admin_key, ADMIN_KEY):
        raise HTTPException(403, "forbidden")

def _now(): return datetime.now(timezone.utc)
def _validate_username(username: str) -> str:
    username = username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", username):
        raise HTTPException(422, "invalid_username")
    return username
def _validate_password(password: str):
    if not isinstance(password, str) or len(password) < 12 or len(password) > 256:
        raise HTTPException(422, "password_must_be_12_to_256_characters")
def _validate_hwid(hwid: str):
    if not isinstance(hwid, str) or not re.fullmatch(r"[a-f0-9]{32,128}", hwid):
        raise HTTPException(422, "invalid_device")
def _active(user):
    end = user.subscription_end
    if end and end.tzinfo is None: end = end.replace(tzinfo=timezone.utc)
    return bool(not user.blocked and end and end > _now())
def _ensure_active(user):
    if not _active(user): raise HTTPException(403, "license_inactive")
def _allowed(user, item):
    return PREMIUM_FOR_ALL and item["feature"] == "premium" or bool((user.features or {}).get(item["feature"]))
def _allowed_plugins(user):
    return [{"id": p, "name": x["name"], "version": x["version"], "plan": "Premium"}
            for p, x in PLUGIN_CATALOG.items() if _allowed(user, x)]
def _token(user):
    return jwt.encode({"uid": user.id, "hwid": user.hwid, "jti": secrets.token_hex(16),
                       "iat": _now(), "exp": _now() + timedelta(hours=SESSION_HOURS)}, JWT_SECRET, algorithm="HS256")
def _token_user(token: str, hwid: str, db):
    _validate_hwid(hwid)
    try: data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"], options={"require": ["exp", "iat", "jti"]})
    except jwt.PyJWTError: raise HTTPException(401, "invalid_session")
    user = db.get(User, data.get("uid"))
    if not user or not hmac.compare_digest(str(data.get("hwid", "")), hwid) or user.hwid != hwid:
        raise HTTPException(401, "invalid_session")
    return user

def _body(request: Request, fields: set):
    # Parsing JSON through FastAPI would be equally valid; this keeps strict fields explicit.
    pass

@app.post("/api/login")
async def login(request: Request, db=Depends(get_db)):
    _rate_limit("login", _client_ip(request), 8, 300)
    try: req = await request.json(); username = _validate_username(str(req.get("username", ""))); password = req.get("password", ""); hwid = req.get("hwid", "")
    except HTTPException: raise
    except Exception: raise HTTPException(422, "invalid_request")
    _validate_password(password); _validate_hwid(hwid)
    user = db.query(User).filter_by(username=username).first()
    # Verify a bcrypt hash even for nonexistent names, reducing username-timing leakage.
    valid = bcrypt.verify(password, user.password_hash) if user else bcrypt.verify(password, bcrypt.hash("dummy-password-not-valid"))
    if not user or not valid: raise HTTPException(401, "invalid_credentials")
    _ensure_active(user)
    if user.hwid is None:
        user.hwid = hwid; db.commit(); db.refresh(user)
    elif not hmac.compare_digest(user.hwid, hwid): raise HTTPException(403, "device_mismatch")
    return {"status": "ok", "session_token": _token(user), "subscription_end": user.subscription_end.isoformat(), "plan": "Premium", "features": user.features, "plugins": _allowed_plugins(user)}

@app.post("/api/heartbeat")
async def heartbeat(request: Request, db=Depends(get_db)):
    req = await request.json(); user = _token_user(req.get("session_token", ""), req.get("hwid", ""), db); _ensure_active(user)
    return {"status": "ok", "subscription_end": user.subscription_end.isoformat(), "plugins": _allowed_plugins(user)}
@app.post("/api/plugins")
async def plugins(request: Request, db=Depends(get_db)):
    req = await request.json(); user = _token_user(req.get("session_token", ""), req.get("hwid", ""), db); _ensure_active(user)
    return {"status": "ok", "plugins": _allowed_plugins(user)}
@app.post("/api/plugins/{plugin_id}")
async def download_plugin(plugin_id: str, request: Request, db=Depends(get_db)):
    req = await request.json(); user = _token_user(req.get("session_token", ""), req.get("hwid", ""), db); _ensure_active(user)
    item = PLUGIN_CATALOG.get(plugin_id)
    if not item: raise HTTPException(404, "plugin_not_found")
    if not _allowed(user, item): raise HTTPException(403, "plugin_not_allowed")
    path = (PLUGINS_DIR / item["filename"]).resolve()
    if PLUGINS_DIR.resolve() not in path.parents or not path.is_file(): raise HTTPException(503, "plugin_unavailable")
    raw = path.read_text(encoding="utf-8-sig").encode("utf-8")
    return {"status": "ok", "id": plugin_id, "name": item["name"], "version": item["version"], "payload_b64": base64.b64encode(zlib.compress(raw, 9)).decode(), "sha256": hashlib.sha256(raw).hexdigest()}

@app.post("/api/admin/users", dependencies=[Depends(require_admin)])
async def create_user(request: Request, db=Depends(get_db)):
    req = await request.json(); username = _validate_username(str(req.get("username", ""))); password = req.get("password", ""); _validate_password(password)
    try: days = int(req.get("days", 30))
    except (TypeError, ValueError): raise HTTPException(422, "invalid_days")
    if not 1 <= days <= 3650: raise HTTPException(422, "invalid_days")
    if db.query(User).filter_by(username=username).first(): raise HTTPException(409, "username_taken")
    features = req.get("features", {"premium": True})
    if not isinstance(features, dict) or len(features) > 20: raise HTTPException(422, "invalid_features")
    user = User(username=username, password_hash=bcrypt.hash(password), subscription_end=_now()+timedelta(days=days), features=features)
    db.add(user); db.commit(); return {"status": "ok", "id": user.id}

@app.get("/api/admin/users", dependencies=[Depends(require_admin)])
def list_users(db=Depends(get_db)):
    return [{"id":u.id,"username":u.username,"blocked":u.blocked,"subscription_end":u.subscription_end.isoformat() if u.subscription_end else None,"hwid":bool(u.hwid),"features":u.features} for u in db.query(User).order_by(User.id).all()]
def _admin_user(user_id, db):
    u=db.get(User,user_id)
    if not u: raise HTTPException(404,"not_found")
    return u
@app.post("/api/admin/users/{user_id}/block", dependencies=[Depends(require_admin)])
def block(user_id:int, db=Depends(get_db)): u=_admin_user(user_id,db); u.blocked=True; db.commit(); return {"status":"ok"}
@app.post("/api/admin/users/{user_id}/unblock", dependencies=[Depends(require_admin)])
def unblock(user_id:int, db=Depends(get_db)): u=_admin_user(user_id,db); u.blocked=False; db.commit(); return {"status":"ok"}
@app.post("/api/admin/users/{user_id}/reset-hwid", dependencies=[Depends(require_admin)])
def reset_hwid(user_id:int, db=Depends(get_db)): u=_admin_user(user_id,db); u.hwid=None; db.commit(); return {"status":"ok"}
@app.post("/api/admin/users/{user_id}/extend", dependencies=[Depends(require_admin)])
async def extend(user_id:int, request:Request, db=Depends(get_db)):
    req=await request.json()
    try: days=int(req.get("days"))
    except (TypeError,ValueError): raise HTTPException(422,"invalid_days")
    if not 1<=days<=3650: raise HTTPException(422,"invalid_days")
    u=_admin_user(user_id,db); end=u.subscription_end.replace(tzinfo=timezone.utc) if u.subscription_end and u.subscription_end.tzinfo is None else u.subscription_end; u.subscription_end=(end if end and end>_now() else _now())+timedelta(days=days); db.commit(); return {"status":"ok","subscription_end":u.subscription_end.isoformat()}
@app.delete("/api/admin/users/{user_id}", dependencies=[Depends(require_admin)])
def delete(user_id:int, db=Depends(get_db)): u=_admin_user(user_id,db); db.delete(u); db.commit(); return {"status":"ok"}
@app.get("/")
def root(): return {"status":"ok"}
