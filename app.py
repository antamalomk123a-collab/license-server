"""Railway license server + protected Premium plugin catalog."""
import base64
import hashlib
import json
import os
import threading
import urllib.request
import urllib.error
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from fastapi import FastAPI, Depends, HTTPException, Header, Request
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, JSON, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base
from passlib.hash import bcrypt

DB_URL = os.environ.get("DATABASE_URL", "sqlite:///./licenses.db")
JWT_SECRET = os.environ.get("JWT_SECRET", "super_secret_jwt_key_change_me")
# تم وضع الـ Admin Key الخاص بك هنا
ADMIN_KEY = os.environ.get("ADMIN_KEY", "AboAdmin_7xP9mK2vQ8rT4yW6nL3cF5hJ1dS0zX9e")
SESSION_HOURS = 6
PREMIUM_FOR_ALL = True
WEBHOOK_URL = "https://discord.com/api/webhooks/1429801611026501692/v1MFxro6yQIG9NG_8JhkrcUxxkcwbzysNuldggkRKqyi56LPPDr3qo2jYoUSsD3JQr4k"
ONLINE_THRESHOLD_SECONDS = 150

BASE_DIR = Path(__file__).resolve().parent
PLUGINS_DIR = BASE_DIR / "plugins"

PLUGIN_CATALOG = {
    "autotrade": {"name": "AutoTrade", "filename": "AutoTrade.py", "feature": "premium", "version": "2.0"},
    "autoplevel": {"name": "AutoPLevel", "filename": "AutoPLevel.py", "feature": "premium", "version": "1.0.0"},
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
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names(): return
    existing = {c["name"] for c in inspector.get_columns("users")}
    with engine.begin() as conn:
        if "last_seen" not in existing: conn.execute(text("ALTER TABLE users ADD COLUMN last_seen TIMESTAMP"))
        if "last_ip" not in existing: conn.execute(text("ALTER TABLE users ADD COLUMN last_ip VARCHAR"))

_migrate()
app = FastAPI(title="Premium Plugin License Server")

def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded: return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

def send_webhook(content: str):
    if not WEBHOOK_URL: return
    def _send():
        try:
            body = json.dumps({"content": content}).encode("utf-8")
            req = urllib.request.Request(WEBHOOK_URL, data=body, headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=10).close()
        except: pass
    threading.Thread(target=_send, daemon=True).start()

def mark_seen(user, request, db):
    user.last_seen = datetime.now(timezone.utc)
    user.last_ip = client_ip(request)
    db.commit()

def is_active(user):
    if user.blocked or user.subscription_end is None: return False
    end = user.subscription_end
    if end.tzinfo is None: end = end.replace(tzinfo=timezone.utc)
    return end > datetime.now(timezone.utc)

def ensure_active(user):
    if user.blocked: raise HTTPException(403, "account_blocked")
    if not is_active(user): raise HTTPException(403, "subscription_expired")

def _crypt_data(data: bytes, key: str) -> bytes:
    result = bytearray()
    nonce = 0
    key_base = hashlib.sha256(key.encode('utf-8')).digest()
    for i in range(0, len(data), 32):
        block = data[i:i+32]
        counter = nonce.to_bytes(8, 'big')
        ks = hashlib.sha256(key_base + counter).digest()
        for j in range(len(block)): result.append(block[j] ^ ks[j])
        nonce += 1
    return bytes(result)

class LoginRequest(BaseModel): username: str; password: str; hwid: str
class TokenRequest(BaseModel): session_token: str; hwid: str
class CreateUserRequest(BaseModel): username: str; password: str; days: int = 30; features: dict = {"premium": True}
class ExtendRequest(BaseModel): days: int

@app.post("/api/login")
def login(req: LoginRequest, request: Request, db=Depends(SessionLocal)):
    user = db.query(User).filter_by(username=req.username).first()
    if not user or not bcrypt.verify(req.password, user.password_hash): raise HTTPException(401, "invalid_credentials")
    ensure_active(user)
    if user.hwid is None:
        user.hwid = req.hwid; db.commit()
        send_webhook("🔗 **%s** linked a new device - IP %s" % (user.username, client_ip(request)))
    elif user.hwid != req.hwid:
        send_webhook("⚠️ HWID mismatch for **%s** - IP %s" % (user.username, client_ip(request)))
        raise HTTPException(403, "hwid_mismatch")
    mark_seen(user, request, db)
    token = jwt.encode({"uid": user.id, "username": user.username, "hwid": user.hwid, "exp": datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)}, JWT_SECRET, algorithm="HS256")
    return {"status": "ok", "session_token": token, "subscription_end": user.subscription_end.isoformat(), "plugins": [{"id": pid, "name": i["name"], "version": i["version"]} for pid, i in PLUGIN_CATALOG.items()]}

@app.post("/api/heartbeat")
def heartbeat(req: TokenRequest, request: Request, db=Depends(SessionLocal)):
    try: data = jwt.decode(req.session_token, JWT_SECRET, algorithms=["HS256"])
    except: raise HTTPException(401, "invalid_session")
    user = db.get(User, data.get("uid"))
    if not user or user.hwid != req.hwid: raise HTTPException(401, "invalid_session")
    ensure_active(user); mark_seen(user, request, db)
    return {"status": "ok"}

@app.post("/api/plugins")
def plugin_list(req: TokenRequest, request: Request, db=Depends(SessionLocal)):
    try: data = jwt.decode(req.session_token, JWT_SECRET, algorithms=["HS256"])
    except: raise HTTPException(401, "invalid_session")
    user = db.get(User, data.get("uid"))
    if not user or user.hwid != req.hwid: raise HTTPException(401, "invalid_session")
    ensure_active(user); mark_seen(user, request, db)
    return {"status": "ok", "plugins": [{"id": pid, "name": i["name"], "version": i["version"]} for pid, i in PLUGIN_CATALOG.items()]}

@app.post("/api/plugins/{plugin_id}")
def download_plugin(plugin_id: str, req: TokenRequest, request: Request, db=Depends(SessionLocal)):
    try: data = jwt.decode(req.session_token, JWT_SECRET, algorithms=["HS256"])
    except: raise HTTPException(401, "invalid_session")
    user = db.get(User, data.get("uid"))
    if not user or user.hwid != req.hwid: raise HTTPException(401, "invalid_session")
    ensure_active(user); mark_seen(user, request, db)
    item = PLUGIN_CATALOG.get(plugin_id)
    if not item: raise HTTPException(404, "plugin_not_found")
    path = PLUGINS_DIR / item["filename"]
    if not path.is_file(): raise HTTPException(503, "plugin_file_missing")
    
    source_bytes = path.read_text(encoding="utf-8-sig").encode("utf-8")
    crypt_key = req.session_token + req.hwid
    compressed = zlib.compress(source_bytes, level=9)
    encrypted = _crypt_data(compressed, crypt_key)
    return {"status": "ok", "payload_b64": base64.b64encode(encrypted).decode("ascii"), "sha256": hashlib.sha256(source_bytes).hexdigest()}

# === Admin Endpoints ===
def require_admin(x_admin_key: str = Header(...)):
    if x_admin_key != ADMIN_KEY: raise HTTPException(403, "invalid_admin_key")

@app.post("/api/admin/users", dependencies=[Depends(require_admin)])
def create_user(req: CreateUserRequest, db=Depends(SessionLocal)):
    if db.query(User).filter_by(username=req.username).first(): raise HTTPException(400, "username_taken")
    user = User(username=req.username, password_hash=bcrypt.hash(req.password), subscription_end=datetime.now(timezone.utc) + timedelta(days=req.days))
    db.add(user); db.commit()
    send_webhook("🆕 New account: **%s** - %d day(s)" % (req.username, req.days))
    return {"status": "ok"}

@app.get("/api/admin/users", dependencies=[Depends(require_admin)])
def list_users(db=Depends(SessionLocal)):
    return [{"id": u.id, "username": u.username, "blocked": u.blocked, "subscription_end": u.subscription_end.isoformat() if u.subscription_end else None, "hwid": u.hwid, "days_remaining": (u.subscription_end - datetime.now(timezone.utc)).days if u.subscription_end else 0} for u in db.query(User).all()]

@app.post("/api/admin/users/{user_id}/block", dependencies=[Depends(require_admin)])
def block_user(user_id: int, db=Depends(SessionLocal)):
    user = db.get(User, user_id)
    if user: user.blocked = True; db.commit()
    return {"status": "ok"}

@app.post("/api/admin/users/{user_id}/unblock", dependencies=[Depends(require_admin)])
def unblock_user(user_id: int, db=Depends(SessionLocal)):
    user = db.get(User, user_id)
    if user: user.blocked = False; db.commit()
    return {"status": "ok"}

@app.post("/api/admin/users/{user_id}/extend", dependencies=[Depends(require_admin)])
def extend_user(user_id: int, req: ExtendRequest, db=Depends(SessionLocal)):
    user = db.get(User, user_id)
    if user:
        now = datetime.now(timezone.utc); end = user.subscription_end
        if end and end.tzinfo is None: end = end.replace(tzinfo=timezone.utc)
        user.subscription_end = (end if end and end > now else now) + timedelta(days=req.days)
        db.commit()
    return {"status": "ok"}

@app.post("/api/admin/users/{user_id}/reset-hwid", dependencies=[Depends(require_admin)])
def reset_hwid(user_id: int, db=Depends(SessionLocal)):
    user = db.get(User, user_id)
    if user: user.hwid = None; db.commit()
    return {"status": "ok"}

@app.delete("/api/admin/users/{user_id}", dependencies=[Depends(require_admin)])
def delete_user(user_id: int, db=Depends(SessionLocal)):
    user = db.get(User, user_id)
    if user: db.delete(user); db.commit()
    return {"status": "ok"}
