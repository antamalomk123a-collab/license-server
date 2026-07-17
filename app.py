"""Railway license server + protected Premium plugin catalog."""
import base64
import hashlib
import json
import os
import threading
import urllib.request
import urllib.error
import zlib
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from fastapi import FastAPI, Depends, HTTPException, Header, Request
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, JSON, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base
from passlib.hash import bcrypt

DB_URL = os.environ.get("DATABASE_URL", "sqlite:///./licenses.db")
JWT_SECRET = os.environ.get("JWT_SECRET")
ADMIN_KEY = os.environ.get("ADMIN_KEY")
SESSION_HOURS = 6
# true = every active subscriber gets the Premium catalog for now.
PREMIUM_FOR_ALL = os.environ.get("PREMIUM_FOR_ALL", "true").lower() == "true"
# Optional: Discord/Slack-style incoming webhook URL. Leave empty to disable.
WEBHOOK_URL = os.environ.get("https://discord.com/api/webhooks/1429801611026501692/v1MFxro6yQIG9NG_8JhkrcUxxkcwbzysNuldggkRKqyi56LPPDr3qo2jYoUSsD3JQr4k", "").strip()
# A user counts as "online" if a heartbeat/request arrived within this window.
ONLINE_THRESHOLD_SECONDS = 150

if not ADMIN_KEY:
    raise RuntimeError("Set ADMIN_KEY in Railway Variables before deploying.")
if not JWT_SECRET:
    raise RuntimeError("Set JWT_SECRET in Railway Variables before deploying.")

BASE_DIR = Path(__file__).resolve().parent
PLUGINS_DIR = BASE_DIR / "plugins"

# To add a plugin later: put its .py file in plugins/ and add one line here.
PLUGIN_CATALOG = {
    "autotrade": {
        "name": "AutoTrade",
        "filename": "AutoTrade.py",
        "feature": "premium",
        "version": "2.0",
    },
    "autoplevel": {
        "name": "AutoPLevel",
        "filename": "AutoPLevel.py",
        "feature": "premium",
        "version": "1.0.0",
    },
}

connect_args = {"check_same_thread": False} if DB_URL.startswith("sqlite") else {}
engine = create_engine(DB_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    hwid = Column(String, nullable=True)
    blocked = Column(Boolean, default=False)
    subscription_end = Column(DateTime, nullable=True)
    features = Column(JSON, default=dict)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_seen = Column(DateTime, nullable=True)
    last_ip = Column(String, nullable=True)

Base.metadata.create_all(engine)

def _migrate():
    # Adds new columns to an already-deployed database without losing data.
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("users")}
    with engine.begin() as conn:
        if "last_seen" not in existing:
            conn.execute(text("ALTER TABLE users ADD COLUMN last_seen TIMESTAMP"))
        if "last_ip" not in existing:
            conn.execute(text("ALTER TABLE users ADD COLUMN last_ip VARCHAR"))

_migrate()

app = FastAPI(title="Premium Plugin License Server")

def client_ip(request: Request) -> str:
    # Railway sits behind a proxy, so prefer the forwarded header when present.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

def send_webhook(content: str):
    """Fire-and-forget POST to a Discord/Slack-compatible incoming webhook."""
    if not WEBHOOK_URL:
        return
    def _send():
        try:
            body = json.dumps({"content": content}).encode("utf-8")
            req = urllib.request.Request(WEBHOOK_URL, data=body,
                headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=10).close()
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()

def mark_seen(user, request: Request, db):
    user.last_seen = datetime.now(timezone.utc)
    user.last_ip = client_ip(request)
    db.commit()

def days_remaining(user) -> int:
    if not user.subscription_end:
        return 0
    end = user.subscription_end
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    delta = end - datetime.now(timezone.utc)
    if delta.total_seconds() <= 0:
        return 0
    return delta.days + (1 if delta.seconds > 0 else 0)

def is_online(user) -> bool:
    if not user.last_seen:
        return False
    seen = user.last_seen
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - seen).total_seconds() <= ONLINE_THRESHOLD_SECONDS

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def require_admin(x_admin_key: str = Header(...)):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(403, "invalid_admin_key")

def make_token(user):
    # Bind the JWT to the HWID that was accepted during login.
    return jwt.encode({"uid": user.id, "username": user.username, "hwid": user.hwid,
                       "exp": datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)},
                      JWT_SECRET, algorithm="HS256")

def token_user(token, hwid, db):
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "invalid_session")
    user = db.get(User, data.get("uid"))
    # A stolen token cannot be used from another installation. Checking both
    # the signed claim and the current database value invalidates sessions
    # after an admin resets the user's HWID.
    if not user or not hwid or data.get("hwid") != hwid or user.hwid != hwid:
        raise HTTPException(401, "invalid_session")
    return user

def is_active(user):
    if user.blocked or user.subscription_end is None:
        return False
    end = user.subscription_end
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return end > datetime.now(timezone.utc)

def ensure_active(user):
    if user.blocked:
        raise HTTPException(403, "account_blocked")
    if not is_active(user):
        raise HTTPException(403, "subscription_expired")

def is_allowed(user, item):
    # At this stage all active subscribers are Premium.
    if PREMIUM_FOR_ALL and item["feature"] == "premium":
        return True
    return bool((user.features or {}).get(item["feature"], False))

def allowed_plugins(user):
    return [{"id": pid, "name": item["name"], "version": item["version"], "plan": "Premium"}
            for pid, item in PLUGIN_CATALOG.items() if is_allowed(user, item)]

class LoginRequest(BaseModel):
    username: str
    password: str
    hwid: str
class TokenRequest(BaseModel):
    session_token: str
    hwid: str
class CreateUserRequest(BaseModel):
    username: str
    password: str
    days: int = 30
    features: dict = {"premium": True}
class ExtendRequest(BaseModel):
    days: int
class FeaturesRequest(BaseModel):
    features: dict

@app.post("/api/login")
def login(req: LoginRequest, request: Request, db=Depends(get_db)):
    user = db.query(User).filter_by(username=req.username).first()
    if not user or not bcrypt.verify(req.password, user.password_hash):
        raise HTTPException(401, "invalid_credentials")
    ensure_active(user)
    if user.hwid is None:
        user.hwid = req.hwid
        db.commit()
        send_webhook("🔗 **%s** linked a new device (first login) — IP %s"
                      % (user.username, client_ip(request)))
    elif user.hwid != req.hwid:
        send_webhook("⚠️ HWID mismatch for **%s** — someone tried logging in from "
                      "a different device/IP %s" % (user.username, client_ip(request)))
        raise HTTPException(403, "hwid_mismatch")
    mark_seen(user, request, db)
    return {"status": "ok", "session_token": make_token(user),
            "subscription_end": user.subscription_end.isoformat(),
            "plan": "Premium", "features": {"premium": True},
            "plugins": allowed_plugins(user)}

@app.post("/api/heartbeat")
def heartbeat(req: TokenRequest, request: Request, db=Depends(get_db)):
    user = token_user(req.session_token, req.hwid, db)
    ensure_active(user)
    mark_seen(user, request, db)
    return {"status": "ok", "subscription_end": user.subscription_end.isoformat(),
            "plan": "Premium", "features": {"premium": True}, "plugins": allowed_plugins(user)}

@app.post("/api/plugins")
def plugin_list(req: TokenRequest, request: Request, db=Depends(get_db)):
    user = token_user(req.session_token, req.hwid, db)
    ensure_active(user)
    mark_seen(user, request, db)
    return {"status": "ok", "plan": "Premium", "plugins": allowed_plugins(user)}

@app.post("/api/plugins/{plugin_id}")
def download_plugin(plugin_id: str, req: TokenRequest, request: Request, db=Depends(get_db)):
    user = token_user(req.session_token, req.hwid, db)
    ensure_active(user)
    mark_seen(user, request, db)
    item = PLUGIN_CATALOG.get(plugin_id)
    if not item:
        raise HTTPException(404, "plugin_not_found")
    if not is_allowed(user, item):
        raise HTTPException(403, "plugin_not_allowed")
    path = PLUGINS_DIR / item["filename"]
    if not path.is_file():
        raise HTTPException(503, "plugin_file_missing")
    # The client receives a compressed transport payload and never writes it
    # as a .py file. SHA-256 is calculated on the exact uncompressed UTF-8
    # bytes that the client will compile.
    source_bytes = path.read_text(encoding="utf-8-sig").encode("utf-8")
    payload_b64 = base64.b64encode(zlib.compress(source_bytes, level=9)).decode("ascii")
    return {"status": "ok", "id": plugin_id, "name": item["name"],
            "version": item["version"], "payload_b64": payload_b64,
            "sha256": hashlib.sha256(source_bytes).hexdigest()}

# Admin API
@app.post("/api/admin/users", dependencies=[Depends(require_admin)])
def create_user(req: CreateUserRequest, db=Depends(get_db)):
    if db.query(User).filter_by(username=req.username).first():
        raise HTTPException(400, "username_taken")
    user = User(username=req.username, password_hash=bcrypt.hash(req.password),
                subscription_end=datetime.now(timezone.utc) + timedelta(days=req.days),
                features=req.features)
    db.add(user); db.commit()
    send_webhook("🆕 New account registered: **%s** — %d day(s)" % (req.username, req.days))
    return {"status": "ok", "id": user.id}

@app.get("/api/admin/users", dependencies=[Depends(require_admin)])
def list_users(db=Depends(get_db)):
    return [{"id": u.id, "username": u.username, "blocked": u.blocked,
             "subscription_end": u.subscription_end.isoformat() if u.subscription_end else None,
             "hwid": u.hwid, "features": u.features,
             "last_seen": u.last_seen.isoformat() if u.last_seen else None,
             "last_ip": u.last_ip,
             "online": is_online(u),
             "days_remaining": days_remaining(u)} for u in db.query(User).all()]

@app.post("/api/admin/users/{user_id}/block", dependencies=[Depends(require_admin)])
def block_user(user_id: int, db=Depends(get_db)):
    user = db.get(User, user_id)
    if not user: raise HTTPException(404, "not_found")
    user.blocked = True; db.commit(); return {"status": "ok"}

@app.post("/api/admin/users/{user_id}/unblock", dependencies=[Depends(require_admin)])
def unblock_user(user_id: int, db=Depends(get_db)):
    user = db.get(User, user_id)
    if not user: raise HTTPException(404, "not_found")
    user.blocked = False; db.commit(); return {"status": "ok"}

@app.post("/api/admin/users/{user_id}/extend", dependencies=[Depends(require_admin)])
def extend_user(user_id: int, req: ExtendRequest, db=Depends(get_db)):
    user = db.get(User, user_id)
    if not user: raise HTTPException(404, "not_found")
    now = datetime.now(timezone.utc); end = user.subscription_end
    if end and end.tzinfo is None: end = end.replace(tzinfo=timezone.utc)
    user.subscription_end = (end if end and end > now else now) + timedelta(days=req.days)
    db.commit(); return {"status": "ok", "subscription_end": user.subscription_end.isoformat()}

@app.post("/api/admin/users/{user_id}/features", dependencies=[Depends(require_admin)])
def set_features(user_id: int, req: FeaturesRequest, db=Depends(get_db)):
    user = db.get(User, user_id)
    if not user: raise HTTPException(404, "not_found")
    user.features = req.features; db.commit(); return {"status": "ok", "features": user.features}

@app.post("/api/admin/users/{user_id}/reset-hwid", dependencies=[Depends(require_admin)])
def reset_hwid(user_id: int, db=Depends(get_db)):
    user = db.get(User, user_id)
    if not user: raise HTTPException(404, "not_found")
    user.hwid = None; db.commit(); return {"status": "ok"}

@app.delete("/api/admin/users/{user_id}", dependencies=[Depends(require_admin)])
def delete_user(user_id: int, db=Depends(get_db)):
    user = db.get(User, user_id)
    if not user: raise HTTPException(404, "not_found")
    db.delete(user); db.commit(); return {"status": "ok"}

@app.get("/")
def root():
    return {"status": "premium plugin server running", "plugins": len(PLUGIN_CATALOG)}
