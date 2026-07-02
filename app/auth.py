from datetime import datetime, timedelta
from fastapi import Request
import bcrypt as _bcrypt
from sqlalchemy.orm import Session

from .models import Staff, LoginState, AuditLog

PIN_MAX_ATTEMPTS = 5
PIN_BASE_LOCKOUT_SECONDS = 60
PIN_MAX_LOCKOUT_SECONDS = 30 * 60


def hash_pin(pin: str) -> str:
    return _bcrypt.hashpw(pin.encode(), _bcrypt.gensalt()).decode()


def verify_pin(pin: str, pin_hash: str) -> bool:
    try:
        return _bcrypt.checkpw(pin.encode(), pin_hash.encode())
    except Exception:
        return False


def get_login_state(db: Session) -> LoginState:
    state = db.get(LoginState, "global")
    if not state:
        state = LoginState(id="global", fail_count=0, lock_until=None)
        db.add(state)
        db.commit()
    return state


def is_locked(db: Session) -> bool:
    state = get_login_state(db)
    return bool(state.lock_until and datetime.utcnow() < state.lock_until)


def lock_seconds_remaining(db: Session) -> int:
    state = get_login_state(db)
    if not state.lock_until:
        return 0
    delta = (state.lock_until - datetime.utcnow()).total_seconds()
    return max(0, int(delta))


def register_pin_failure(db: Session):
    state = get_login_state(db)
    state.fail_count = (state.fail_count or 0) + 1
    if state.fail_count % PIN_MAX_ATTEMPTS == 0:
        lockouts_so_far = state.fail_count // PIN_MAX_ATTEMPTS
        duration = min(PIN_MAX_LOCKOUT_SECONDS, PIN_BASE_LOCKOUT_SECONDS * (2 ** (lockouts_so_far - 1)))
        state.lock_until = datetime.utcnow() + timedelta(seconds=duration)
        db.add(AuditLog(action="LOGIN", staff_name="System",
                         detail="PIN pad locked after repeated failed attempts"))
    db.commit()


def register_pin_success(db: Session):
    state = get_login_state(db)
    state.fail_count = 0
    state.lock_until = None
    db.commit()


def attempt_login(db: Session, pin: str):
    """Returns the matching active Staff, or None. Caller is responsible
    for checking is_locked() before calling this."""
    for staff in db.query(Staff).filter(Staff.active == True).all():  # noqa: E712
        if verify_pin(pin, staff.pin_hash):
            return staff
    return None


def get_current_staff(request: Request, db: Session):
    staff_id = request.session.get("staff_id")
    if not staff_id:
        return None
    return db.get(Staff, staff_id)


def role_allowed(staff, *roles) -> bool:
    if not staff:
        return False
    return staff.role in roles or staff.role == "owner"


def add_audit(db: Session, staff, action: str, detail: str):
    db.add(AuditLog(
        staff_id=staff.id if staff else None,
        staff_name=staff.name if staff else "System",
        action=action, detail=detail,
    ))
    db.commit()

