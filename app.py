"""
License Server — نظام تراخيص واشتراكات بسيط
================================================
- يوثّق المستخدمين (username/password)
- يربط كل حساب بجهاز واحد (HWID)
- يتحكم في الاشتراك (تفعيل / حظر / تمديد) عن طريق Admin endpoints
- يرجّع "feature flags" للـ client بدل ما يبعتله كود ينفّذه (أأمن بكتير)

قبل التشغيل:
  export ADMIN_KEY="اختار مفتاح سري طويل هنا"
  export JWT_SECRET="اختار سر تاني للـ JWT"   (اختياري، بيتولّد تلقائي لو مش موجود)
"""

import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import FastAPI, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, JSON
from sqlalchemy.orm import sessionmaker, declarative_base
from passlib.hash import bcrypt

# ── Config ──────────────────────────────────────────────────────────
DB_URL = os.environ.get("DATABASE_URL", "sqlite:///./licenses.db")
JWT_SECRET = os.environ.get("JWT_SECRET", secrets.token_hex(32))
ADMIN_KEY = os.environ.get("ADMIN_KEY")
SESSION_HOURS = 6

if not ADMIN_KEY:
    raise RuntimeError(
        "لازم تحدد ADMIN_KEY كـ environment variable قبل التشغيل "
        "(ده المفتاح اللي هتستخدمه انت بس عشان تدير الحسابات)."
    )

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
    subscription_end = Column(DateTime, nullable=True)  # None = لسه معملش تفعيل
    features = Column(JSON, default=dict)  # مثال: {"premium_farm": true}
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


Base.metadata.create_all(engine)

app = FastAPI(title="License Server")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_admin(x_admin_key: str = Header(...)):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(403, "invalid_admin_key")


def make_token(user: User) -> str:
    payload = {
        "uid": user.id,
        "username": user.username,
        "exp": datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def decode_token(token: str):
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def is_active(user: User) -> bool:
    if user.blocked:
        return False
    if user.subscription_end is None:
        return False
    end = user.subscription_end
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return end > datetime.now(timezone.utc)


# ── Schemas ─────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str
    hwid: str


class HeartbeatRequest(BaseModel):
    session_token: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    days: int = 30
    features: dict = {}


class ExtendRequest(BaseModel):
    days: int


class FeaturesRequest(BaseModel):
    features: dict


# ── Public endpoints (بيستخدمهم الـ client/plugin) ───────────────────
@app.post("/api/login")
def login(req: LoginRequest, db=Depends(get_db)):
    user = db.query(User).filter_by(username=req.username).first()
    if not user or not bcrypt.verify(req.password, user.password_hash):
        raise HTTPException(401, "invalid_credentials")
    if user.blocked:
        raise HTTPException(403, "account_blocked")
    if not is_active(user):
        raise HTTPException(403, "subscription_expired")

    if user.hwid is None:
        user.hwid = req.hwid
        db.commit()
    elif user.hwid != req.hwid:
        raise HTTPException(403, "hwid_mismatch")

    token = make_token(user)
    return {
        "status": "ok",
        "session_token": token,
        "subscription_end": user.subscription_end.isoformat(),
        "features": user.features or {},
    }


@app.post("/api/heartbeat")
def heartbeat(req: HeartbeatRequest, db=Depends(get_db)):
    data = decode_token(req.session_token)
    if not data:
        raise HTTPException(401, "invalid_session")
    user = db.query(User).get(data["uid"])
    if not user:
        raise HTTPException(401, "invalid_session")
    if user.blocked:
        raise HTTPException(403, "account_blocked")
    if not is_active(user):
        raise HTTPException(403, "subscription_expired")
    return {
        "status": "ok",
        "subscription_end": user.subscription_end.isoformat(),
        "features": user.features or {},
    }


# ── Admin endpoints (تحتاج Header: X-Admin-Key) ──────────────────────
@app.post("/api/admin/users", dependencies=[Depends(require_admin)])
def create_user(req: CreateUserRequest, db=Depends(get_db)):
    if db.query(User).filter_by(username=req.username).first():
        raise HTTPException(400, "username_taken")
    user = User(
        username=req.username,
        password_hash=bcrypt.hash(req.password),
        subscription_end=datetime.now(timezone.utc) + timedelta(days=req.days),
        features=req.features,
    )
    db.add(user)
    db.commit()
    return {"status": "ok", "id": user.id}


@app.get("/api/admin/users", dependencies=[Depends(require_admin)])
def list_users(db=Depends(get_db)):
    users = db.query(User).all()
    return [
        {
            "id": u.id,
            "username": u.username,
            "blocked": u.blocked,
            "subscription_end": u.subscription_end.isoformat() if u.subscription_end else None,
            "hwid": u.hwid,
            "features": u.features,
        }
        for u in users
    ]


@app.post("/api/admin/users/{user_id}/block", dependencies=[Depends(require_admin)])
def block_user(user_id: int, db=Depends(get_db)):
    user = db.query(User).get(user_id)
    if not user:
        raise HTTPException(404, "not_found")
    user.blocked = True
    db.commit()
    return {"status": "ok"}


@app.post("/api/admin/users/{user_id}/unblock", dependencies=[Depends(require_admin)])
def unblock_user(user_id: int, db=Depends(get_db)):
    user = db.query(User).get(user_id)
    if not user:
        raise HTTPException(404, "not_found")
    user.blocked = False
    db.commit()
    return {"status": "ok"}


@app.post("/api/admin/users/{user_id}/extend", dependencies=[Depends(require_admin)])
def extend_user(user_id: int, req: ExtendRequest, db=Depends(get_db)):
    user = db.query(User).get(user_id)
    if not user:
        raise HTTPException(404, "not_found")
    now = datetime.now(timezone.utc)
    current_end = user.subscription_end
    if current_end and current_end.tzinfo is None:
        current_end = current_end.replace(tzinfo=timezone.utc)
    base = current_end if (current_end and current_end > now) else now
    user.subscription_end = base + timedelta(days=req.days)
    db.commit()
    return {"status": "ok", "subscription_end": user.subscription_end.isoformat()}


@app.post("/api/admin/users/{user_id}/features", dependencies=[Depends(require_admin)])
def set_features(user_id: int, req: FeaturesRequest, db=Depends(get_db)):
    user = db.query(User).get(user_id)
    if not user:
        raise HTTPException(404, "not_found")
    user.features = req.features
    db.commit()
    return {"status": "ok", "features": user.features}


@app.post("/api/admin/users/{user_id}/reset-hwid", dependencies=[Depends(require_admin)])
def reset_hwid(user_id: int, db=Depends(get_db)):
    user = db.query(User).get(user_id)
    if not user:
        raise HTTPException(404, "not_found")
    user.hwid = None
    db.commit()
    return {"status": "ok"}


@app.delete("/api/admin/users/{user_id}", dependencies=[Depends(require_admin)])
def delete_user(user_id: int, db=Depends(get_db)):
    user = db.query(User).get(user_id)
    if not user:
        raise HTTPException(404, "not_found")
    db.delete(user)
    db.commit()
    return {"status": "ok"}


@app.get("/")
def root():
    return {"status": "license server running"}
