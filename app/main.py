import csv
import io
import json
import os
import secrets
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Depends, Form, UploadFile, File
from fastapi.responses import RedirectResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session

from .database import get_db, SessionLocal
from .models import Staff, Product, Customer, Repair, Invoice, InvoiceLine, AuditLog, Setting, HeldCart, CashSession
from .auth import (
    hash_pin, verify_pin, is_locked, lock_seconds_remaining,
    register_pin_failure, register_pin_success, attempt_login,
    get_current_staff, role_allowed, add_audit,
)
from .tax import calc_canadian_tax, PROVINCE_LABELS
from .repairs_const import STATUS_LABELS, STATUS_ORDER, STATUS_BADGE, ISSUE_TYPES, next_status
from .product_const import CATEGORY_LABELS, CAT_SUBCATEGORIES
from .notifications import send_email_receipt, send_sms, send_plain_email
from apscheduler.schedulers.background import BackgroundScheduler
from .seed import init_db

app = FastAPI(title="TechPro+ CRM")

# Use a persistent secret (set SESSION_SECRET in your environment for
# production) — a fresh random one each start would log every staff
# member out whenever the server restarts.
_SECRET_FILE = os.path.join(os.path.dirname(__file__), "..", ".session_secret")
def _get_session_secret():
    env_secret = os.environ.get("SESSION_SECRET")
    if env_secret:
        return env_secret
    if os.path.exists(_SECRET_FILE):
        with open(_SECRET_FILE) as f:
            return f.read().strip()
    new_secret = secrets.token_hex(32)
    with open(_SECRET_FILE, "w") as f:
        f.write(new_secret)
    return new_secret

app.add_middleware(SessionMiddleware, secret_key=_get_session_secret())
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# WINDOWS FIX: %-d (remove leading zero from day) is Linux-only and
# crashes on Windows with a ValueError. This custom Jinja filter does
# the same thing cross-platform by using %d and stripping manually.
def _datefmt(dt, fmt: str) -> str:
    """Cross-platform date formatting. Use {d} instead of %-d in format
    strings passed to this filter — e.g. '%B {d}, %Y'."""
    result = dt.strftime(fmt.replace("{d}", "%d"))
    result = result.replace(" 0", " ").replace("/0", "/")
    return result

templates.env.filters["datefmt"] = _datefmt

init_db()


# ── helpers ──────────────────────────────────────────────────────────
def get_setting(db: Session, key: str, default=""):
    s = db.get(Setting, key)
    return s.value if s else default


def set_setting(db: Session, key: str, value: str):
    s = db.get(Setting, key)
    if s:
        s.value = value
    else:
        db.add(Setting(key=key, value=value))
    db.commit()


def cart_get(request: Request):
    return request.session.setdefault("cart", [])


def cart_totals(request: Request, db: Session):
    cart = cart_get(request)
    cart_sub = round(sum(i["price"] * i["qty"] for i in cart), 2)

    override = request.session.get("sub_override")
    sub = override if override is not None else cart_sub

    disc_mode = request.session.get("disc_mode", "$")
    disc_raw = request.session.get("disc_value", 0) or 0
    manual_disc = sub * (min(disc_raw, 100) / 100) if disc_mode == "%" else disc_raw

    loyalty_discount = request.session.get("loyalty_discount", 0) or 0
    store_credit_used = request.session.get("store_credit_used", 0) or 0

    disc = round(manual_disc + loyalty_discount + store_credit_used, 2)

    taxable = max(0, sub - disc)
    province = get_setting(db, "province", "ON")
    tax = calc_canadian_tax(taxable, province)
    return {
        "cart": cart, "cart_sub": cart_sub, "sub": round(sub, 2),
        "disc": disc, "disc_mode": disc_mode, "disc_raw": disc_raw,
        "loyalty_discount": loyalty_discount, "store_credit_used": store_credit_used,
        "tax": tax, "total": tax["total"],
    }


def reset_cart_overrides(request: Request):
    """Reset things that should not survive a cart-contents change —
    mirrors the original's behavior of dropping the manual subtotal
    override whenever items are added/removed/changed."""
    request.session["sub_override"] = None


def reset_customer_redemptions(request: Request):
    """Loyalty/store-credit redemptions are tied to a specific customer —
    reset them whenever the customer attached to the sale changes."""
    request.session["loyalty_discount"] = 0
    request.session["store_credit_used"] = 0


def next_invoice_number(db: Session):
    prefix = get_setting(db, "invoice_prefix", "INV")
    counter = int(get_setting(db, "invoice_counter", "1000"))
    set_setting(db, "invoice_counter", str(counter + 1))
    return f"{prefix}-{counter}"


def require_login(request: Request, db: Session):
    staff = get_current_staff(request, db)
    return staff


# ── LOGIN ────────────────────────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    locked = is_locked(db)
    return templates.TemplateResponse(request, "login.html", {
        "locked": locked,
        "lock_seconds": lock_seconds_remaining(db) if locked else 0,
        "error": request.session.pop("login_error", None),
    })


@app.post("/login")
def login_submit(request: Request, pin: str = Form(...), db: Session = Depends(get_db)):
    if is_locked(db):
        request.session["login_error"] = f"Locked. Try again in {lock_seconds_remaining(db)}s."
        return RedirectResponse("/login", status_code=303)

    staff = attempt_login(db, pin)
    if staff:
        register_pin_success(db)
        request.session["staff_id"] = staff.id
        request.session["last_activity"] = datetime.utcnow().isoformat()
        add_audit(db, staff, "LOGIN", f"Staff login: {staff.name}")
        return RedirectResponse("/", status_code=303)
    else:
        register_pin_failure(db)
        request.session["login_error"] = "Incorrect PIN."
        return RedirectResponse("/login", status_code=303)


@app.get("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    staff = get_current_staff(request, db)
    if staff:
        add_audit(db, staff, "LOGOUT", f"Staff logout: {staff.name}")
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ── DASHBOARD ────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)

    today = datetime.utcnow().date()
    invoices_today = [i for i in db.query(Invoice).all() if i.date.date() == today]
    sales_today = round(sum(i.total for i in invoices_today), 2)
    recent_invoices = db.query(Invoice).order_by(Invoice.date.desc()).limit(8).all()
    low_stock = db.query(Product).filter(Product.stock <= Product.reorder_threshold).all()

    backup_warning = None
    if role_allowed(staff, "owner"):
        last_backup = get_setting(db, "last_backup", "")
        total_customers = db.query(Customer).count()
        total_invoices = db.query(Invoice).count()
        if not last_backup:
            if total_invoices > 0 or total_customers > 2:
                backup_warning = "You have live data but have never exported a backup."
        else:
            days_since = (datetime.utcnow() - datetime.fromisoformat(last_backup)).days
            if days_since >= 7:
                backup_warning = f"Last backup was {days_since} day{'s' if days_since != 1 else ''} ago."

    return templates.TemplateResponse(request, "dashboard.html", {
        "staff": staff,
        "sales_today": sales_today, "count_today": len(invoices_today),
        "recent_invoices": recent_invoices, "low_stock": low_stock,
        "shop_name": get_setting(db, "shop_name", "TechPro+"),
        "backup_warning": backup_warning,
    })


# ── POS ──────────────────────────────────────────────────────────────
@app.get("/pos", response_class=HTMLResponse)
def pos_page(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    products = db.query(Product).all()
    customers = db.query(Customer).order_by(Customer.name).all()
    totals = cart_totals(request, db)
    customer_id = request.session.get("customer_id")
    selected_customer = db.get(Customer, customer_id) if customer_id else None
    held_carts = db.query(HeldCart).order_by(HeldCart.created_at.desc()).all()
    points_redeem_rate = float(get_setting(db, "points_redeem_rate", "100"))
    all_brands = sorted({p.subcategory for p in products if p.subcategory})
    return templates.TemplateResponse(request, "pos.html", {
        "staff": staff, "products": products,
        "customers": customers, "customer_id": customer_id,
        "selected_customer": selected_customer, "held_carts": held_carts,
        "points_redeem_rate": points_redeem_rate,
        "scan_error": request.session.pop("scan_error", None),
        "category_labels": CATEGORY_LABELS, "all_brands": all_brands,
        **totals,
    })


@app.post("/pos/scan")
def pos_scan(request: Request, sku: str = Form(...), db: Session = Depends(get_db)):
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    needle = sku.strip().upper()
    if not needle:
        return RedirectResponse("/pos", status_code=303)
    product = db.query(Product).filter(Product.sku.ilike(needle)).first()
    if not product:
        product = db.query(Product).filter(Product.name.ilike(f"%{needle}%")).first()
    if not product:
        request.session["scan_error"] = f'No product found for "{sku}"'
        return RedirectResponse("/pos", status_code=303)

    cart = cart_get(request)
    for item in cart:
        if item["product_id"] == product.id:
            item["qty"] += 1
            break
    else:
        cart.append({"product_id": product.id, "name": product.name, "sku": product.sku, "price": product.price, "qty": 1})
    request.session["cart"] = cart
    request.session["sub_override"] = None
    return RedirectResponse("/pos", status_code=303)


@app.post("/pos/add/{product_id}")
def pos_add(request: Request, product_id: str, db: Session = Depends(get_db)):
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    product = db.get(Product, product_id)
    if product:
        cart = cart_get(request)
        for item in cart:
            if item["product_id"] == product_id:
                item["qty"] += 1
                break
        else:
            cart.append({"product_id": product.id, "name": product.name, "sku": product.sku, "price": product.price, "qty": 1})
        request.session["cart"] = cart
        request.session["sub_override"] = None  # cart changed — drop any manual override
    return RedirectResponse("/pos", status_code=303)


@app.post("/pos/qty/{idx}")
def pos_qty(request: Request, idx: int, qty: int = Form(...), db: Session = Depends(get_db)):
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    cart = cart_get(request)
    if 0 <= idx < len(cart):
        if qty <= 0:
            cart.pop(idx)
        else:
            cart[idx]["qty"] = qty
        request.session["cart"] = cart
        request.session["sub_override"] = None
    return RedirectResponse("/pos", status_code=303)


@app.post("/pos/remove/{idx}")
def pos_remove(request: Request, idx: int, db: Session = Depends(get_db)):
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    cart = cart_get(request)
    if 0 <= idx < len(cart):
        cart.pop(idx)
        request.session["cart"] = cart
        request.session["sub_override"] = None
    return RedirectResponse("/pos", status_code=303)


@app.post("/pos/clear")
def pos_clear(request: Request, db: Session = Depends(get_db)):
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    request.session["cart"] = []
    request.session["sub_override"] = None
    request.session["disc_value"] = 0
    request.session["disc_mode"] = "$"
    request.session["customer_id"] = None
    reset_customer_redemptions(request)
    return RedirectResponse("/pos", status_code=303)


@app.post("/pos/subtotal")
def pos_subtotal(request: Request, value: float = Form(...), db: Session = Depends(get_db)):
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    request.session["sub_override"] = max(0, value)
    return RedirectResponse("/pos", status_code=303)


@app.post("/pos/discount")
def pos_discount(request: Request, mode: str = Form(...), value: float = Form(0), db: Session = Depends(get_db)):
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    request.session["disc_mode"] = mode if mode in ("$", "%") else "$"
    request.session["disc_value"] = max(0, value)
    return RedirectResponse("/pos", status_code=303)


@app.post("/pos/customer")
def pos_customer(request: Request, customer_id: str = Form(""), db: Session = Depends(get_db)):
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    request.session["customer_id"] = customer_id or None
    reset_customer_redemptions(request)  # redemptions are tied to whoever was previously attached
    return RedirectResponse("/pos", status_code=303)


@app.post("/pos/redeem-points")
def pos_redeem_points(request: Request, points: int = Form(...), db: Session = Depends(get_db)):
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    customer_id = request.session.get("customer_id")
    customer = db.get(Customer, customer_id) if customer_id else None
    rate = float(get_setting(db, "points_redeem_rate", "100"))
    if customer:
        # Round down to a whole multiple of the redemption rate, same as the original
        pts = (points // int(rate)) * int(rate)
        if 0 < pts <= (customer.points or 0):
            dollar_value = pts / rate
            request.session["loyalty_discount"] = (request.session.get("loyalty_discount", 0) or 0) + dollar_value
    return RedirectResponse("/pos", status_code=303)


@app.post("/pos/redeem-credit")
def pos_redeem_credit(request: Request, amount: float = Form(...), db: Session = Depends(get_db)):
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    customer_id = request.session.get("customer_id")
    customer = db.get(Customer, customer_id) if customer_id else None
    if customer and 0 < amount <= (customer.store_credit or 0):
        request.session["store_credit_used"] = (request.session.get("store_credit_used", 0) or 0) + amount
    return RedirectResponse("/pos", status_code=303)


@app.post("/pos/hold")
def pos_hold(request: Request, name: str = Form(""), db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    cart = cart_get(request)
    if not cart:
        return RedirectResponse("/pos", status_code=303)
    held = HeldCart(
        name=name.strip() or f"Hold {datetime.utcnow().strftime('%H:%M:%S')}",
        cart_json=json.dumps(cart),
        customer_id=request.session.get("customer_id"),
        disc_mode=request.session.get("disc_mode", "$"),
        disc_value=request.session.get("disc_value", 0) or 0,
    )
    db.add(held)
    add_audit(db, staff, "HOLD_CART", f"Cart held: {held.name}")
    db.commit()

    request.session["cart"] = []
    request.session["sub_override"] = None
    request.session["disc_value"] = 0
    request.session["disc_mode"] = "$"
    request.session["customer_id"] = None
    reset_customer_redemptions(request)
    return RedirectResponse("/pos", status_code=303)


@app.post("/pos/recall/{held_id}")
def pos_recall(request: Request, held_id: str, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    held = db.get(HeldCart, held_id)
    if held:
        request.session["cart"] = json.loads(held.cart_json)
        request.session["sub_override"] = None
        request.session["customer_id"] = held.customer_id
        request.session["disc_mode"] = held.disc_mode
        request.session["disc_value"] = held.disc_value
        reset_customer_redemptions(request)
        add_audit(db, staff, "RECALL_CART", f"Cart recalled: {held.name}")
        db.delete(held)
        db.commit()
    return RedirectResponse("/pos", status_code=303)


@app.post("/pos/held/{held_id}/delete")
def pos_held_delete(request: Request, held_id: str, db: Session = Depends(get_db)):
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    held = db.get(HeldCart, held_id)
    if held:
        db.delete(held)
        db.commit()
    return RedirectResponse("/pos", status_code=303)


@app.post("/pos/checkout")
def pos_checkout(request: Request, payment_method: str = Form("Cash"),
                  tendered: float = Form(0), db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)

    cart = cart_get(request)
    if not cart:
        return RedirectResponse("/pos", status_code=303)

    # Recompute fresh, server-side, right before charging — this is the
    # authoritative total. Nothing the browser sends is trusted directly;
    # prices come from the DB-backed session cart, not from a hidden form field.
    totals = cart_totals(request, db)
    total = totals["total"]

    if payment_method == "Cash" and tendered > 0 and tendered < total:
        return HTMLResponse(f"Tendered amount (${tendered:.2f}) is less than the total (${total:.2f}).", status_code=400)
    change_given = max(0, round(tendered - total, 2)) if tendered > 0 else 0

    customer_id = request.session.get("customer_id")
    customer = db.get(Customer, customer_id) if customer_id else None

    loyalty_discount = totals["loyalty_discount"]
    store_credit_used = totals["store_credit_used"]
    rate = float(get_setting(db, "points_redeem_rate", "100"))
    loyalty_pts_used = round(loyalty_discount * rate)

    invoice = Invoice(
        number=next_invoice_number(db),
        customer_id=customer.id if customer else None,
        staff_id=staff.id,
        payment_method=payment_method,
        subtotal=totals["sub"],
        discount=totals["disc"],
        loyalty_pts_used=loyalty_pts_used,
        store_credit_used=store_credit_used,
        tendered=tendered,
        change_given=change_given,
        tax_breakdown=json.dumps(totals["tax"]["lines"]),
        tax_total=totals["tax"]["tax_total"],
        total=total,
    )
    db.add(invoice)
    db.flush()

    for item in cart:
        db.add(InvoiceLine(invoice_id=invoice.id, product_id=item["product_id"],
                            name=item["name"], sku=item.get("sku", ""), qty=item["qty"], price=item["price"]))
        product = db.get(Product, item["product_id"])
        if product:
            product.stock = max(0, product.stock - item["qty"])

    if customer:
        # Spend redemptions first (mirrors the original's order of operations)
        if store_credit_used > 0:
            customer.store_credit = round((customer.store_credit or 0) - store_credit_used, 2)
            add_audit(db, staff, "STORE_CREDIT", f"Redeemed ${store_credit_used:.2f} store credit for {customer.name}")
        if loyalty_pts_used > 0:
            customer.points = max(0, (customer.points or 0) - loyalty_pts_used)
            add_audit(db, staff, "LOYALTY", f"Redeemed {loyalty_pts_used} points for ${loyalty_discount:.2f} — {customer.name}")
        # Earn new points on the final total
        points_per_dollar = float(get_setting(db, "points_per_dollar", "1"))
        customer.points = (customer.points or 0) + int(total * points_per_dollar)
        customer.spent = round((customer.spent or 0) + total, 2)
        customer.last_visit = datetime.utcnow().isoformat()

    add_audit(db, staff, "INVOICE_CREATE", f"Invoice {invoice.number} — ${total:.2f} — {payment_method}")
    db.commit()

    request.session["cart"] = []
    request.session["sub_override"] = None
    request.session["disc_value"] = 0
    request.session["disc_mode"] = "$"
    request.session["customer_id"] = None
    reset_customer_redemptions(request)
    request.session["flash"] = ("green", f"✓ Sale complete — Invoice {invoice.number} for ${total:.2f}. "
                                          f"Use the buttons below to email or print the receipt.")
    return RedirectResponse(f"/invoices/{invoice.id}", status_code=303)


# ── INVOICES ─────────────────────────────────────────────────────────
@app.get("/invoices", response_class=HTMLResponse)
def invoices_list(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    invoices = db.query(Invoice).order_by(Invoice.date.desc()).limit(100).all()
    return templates.TemplateResponse(request, "invoices.html", {"staff": staff, "invoices": invoices})


def get_shop_info(db: Session):
    return {
        "name": get_setting(db, "shop_name", "TechPro+"),
        "address": get_setting(db, "shop_address", ""),
        "phone": get_setting(db, "shop_phone", ""),
        "email": get_setting(db, "shop_email", ""),
        "gst": get_setting(db, "shop_gst", ""),
        "pst": get_setting(db, "shop_pst", ""),
    }


@app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_detail(request: Request, invoice_id: str, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    invoice = db.get(Invoice, invoice_id)
    tax_lines = json.loads(invoice.tax_breakdown) if invoice.tax_breakdown else []
    can_refund = role_allowed(staff, "owner", "manager")
    flash = request.session.pop("flash", None)
    province = get_setting(db, "province", "ON")
    return templates.TemplateResponse(request, "invoice_detail.html", {
        "staff": staff, "invoice": invoice, "shop": get_shop_info(db),
        "province_label": PROVINCE_LABELS.get(province, province),
        "tax_lines": tax_lines, "can_refund": can_refund, "flash": flash,
    })


@app.get("/invoices/{invoice_id}/thermal", response_class=HTMLResponse)
def invoice_thermal(request: Request, invoice_id: str, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        return RedirectResponse("/invoices", status_code=303)
    tax_lines = json.loads(invoice.tax_breakdown) if invoice.tax_breakdown else []
    return templates.TemplateResponse(request, "invoice_thermal.html", {
        "staff": staff, "invoice": invoice, "shop": get_shop_info(db), "tax_lines": tax_lines,
    })


@app.post("/invoices/{invoice_id}/refund")
def invoice_refund(request: Request, invoice_id: str, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — refunds require manager or owner role.", status_code=403)

    invoice = db.get(Invoice, invoice_id)
    if invoice and not invoice.refunded:
        invoice.refunded = True
        for line in invoice.lines:
            product = db.get(Product, line.product_id) if line.product_id else None
            if product:
                product.stock += line.qty
        if invoice.customer_id:
            customer = db.get(Customer, invoice.customer_id)
            if customer:
                customer.spent = round((customer.spent or 0) - invoice.total, 2)
        add_audit(db, staff, "REFUND", f"Refunded {invoice.number} — ${invoice.total:.2f}")
        db.commit()
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


@app.post("/invoices/{invoice_id}/email")
def invoice_email(request: Request, invoice_id: str, to_email: str = Form(""), db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        return RedirectResponse("/invoices", status_code=303)
    recipient = to_email or (invoice.customer.email if invoice.customer else "")
    if not recipient:
        request.session["flash"] = ("red", "No email address on file for this customer.")
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)

    ok, msg = send_email_receipt(db, invoice, recipient, get_setting)
    if ok:
        add_audit(db, staff, "EMAIL_RECEIPT", f"Receipt emailed to {recipient} for invoice {invoice.number}")
    request.session["flash"] = ("green" if ok else "red", msg)
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


@app.post("/invoices/{invoice_id}/sms")
def invoice_sms(request: Request, invoice_id: str, to_phone: str = Form(""), db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        return RedirectResponse("/invoices", status_code=303)
    recipient = to_phone or (invoice.customer.phone if invoice.customer else "")
    if not recipient:
        request.session["flash"] = ("red", "No phone number on file for this customer.")
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)

    shop_name = get_setting(db, "shop_name", "the shop")
    message = (f"Receipt from {shop_name} — Invoice {invoice.number}, total ${invoice.total:.2f}. "
               f"Thanks for your business!")
    ok, msg = send_sms(db, recipient, message, get_setting)
    if ok:
        add_audit(db, staff, "SMS_RECEIPT", f"Receipt SMS sent to {recipient} for invoice {invoice.number}")
    request.session["flash"] = ("green" if ok else "red", msg)
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


# ── PRODUCTS ─────────────────────────────────────────────────────────
@app.get("/products", response_class=HTMLResponse)
def products_list(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    products = db.query(Product).order_by(Product.name).all()
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "products.html", {
        "staff": staff, "products": products, "flash": flash,
        "category_labels": CATEGORY_LABELS, "cat_subcategories": CAT_SUBCATEGORIES,
    })


@app.post("/products/import")
async def products_import(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — bulk import requires manager or owner role.", status_code=403)

    raw = (await file.read()).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(raw))
    # Tolerant of different header casing/spacing, and matches our own CSV export columns
    def norm(d):
        return {k.strip().lower(): v for k, v in d.items()}

    existing_skus = {p.sku for p in db.query(Product).all() if p.sku}
    added, skipped, errors = 0, 0, 0
    for row in reader:
        row = norm(row)
        sku = (row.get("sku") or "").strip()
        name = (row.get("name") or "").strip()
        if not sku or not name:
            errors += 1
            continue
        if sku in existing_skus:
            skipped += 1
            continue
        try:
            price = float(row.get("price") or 0)
            cost = float(row.get("cost") or 0)
            stock = int(float(row.get("stock") or 0))
        except ValueError:
            errors += 1
            continue
        db.add(Product(sku=sku, name=name, category=(row.get("category") or "").strip(),
                        subcategory=(row.get("brand") or row.get("subcategory") or "").strip(),
                        price=price, cost=cost, stock=stock))
        existing_skus.add(sku)
        added += 1

    add_audit(db, staff, "PRODUCT_IMPORT", f"CSV import: {added} added, {skipped} skipped (duplicate SKU), {errors} invalid rows")
    db.commit()
    request.session["flash"] = ("green" if errors == 0 else "amber",
                                 f"Imported {added} new products. {skipped} skipped as duplicates. {errors} rows had errors.")
    return RedirectResponse("/products", status_code=303)


@app.post("/products/add")
def product_add(request: Request, sku: str = Form(...), name: str = Form(...),
                 category: str = Form(""), subcategory: str = Form(""), price: float = Form(0),
                 cost: float = Form(0), stock: int = Form(0), reorder_threshold: int = Form(5),
                 reorder_qty: int = Form(10), db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    db.add(Product(sku=sku, name=name, category=category, subcategory=subcategory,
                    price=price, cost=cost, stock=stock,
                    reorder_threshold=reorder_threshold, reorder_qty=reorder_qty))
    add_audit(db, staff, "PRODUCT_ADD", f"Added product: {name}")
    db.commit()
    return RedirectResponse("/products", status_code=303)


@app.get("/products/reorder", response_class=HTMLResponse)
def reorder_list(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    low_stock = (db.query(Product)
                 .filter(Product.stock <= Product.reorder_threshold)
                 .order_by(Product.stock).all())
    total_cost = round(sum(p.cost * p.reorder_qty for p in low_stock), 2)
    return templates.TemplateResponse(request, "reorder_list.html", {
        "staff": staff, "products": low_stock, "total_cost": total_cost,
    })


@app.get("/products/reorder/print", response_class=HTMLResponse)
def reorder_list_print(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    low_stock = (db.query(Product)
                 .filter(Product.stock <= Product.reorder_threshold)
                 .order_by(Product.category, Product.name).all())
    total_cost = round(sum(p.cost * p.reorder_qty for p in low_stock), 2)
    shop_name = get_setting(db, "shop_name", "TechPro+")
    return templates.TemplateResponse(request, "reorder_print.html", {
        "staff": staff, "products": low_stock, "total_cost": total_cost, "shop_name": shop_name,
    })


@app.get("/export/csv/reorder")
def export_reorder_csv(request: Request, db: Session = Depends(get_db)):
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    low_stock = db.query(Product).filter(Product.stock <= Product.reorder_threshold).order_by(Product.name).all()
    rows = [(p.sku, p.name, p.category, p.subcategory, p.stock, p.reorder_threshold, p.reorder_qty, round(p.cost * p.reorder_qty, 2)) for p in low_stock]
    return _csv_response(rows, ["SKU", "Name", "Category", "Brand", "Current Stock", "Reorder At", "Suggested Qty", "Est. Cost"], "reorder_list.csv")


@app.get("/products/{product_id}/edit", response_class=HTMLResponse)
def product_edit_page(request: Request, product_id: str, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    product = db.get(Product, product_id)
    if not product:
        return RedirectResponse("/products", status_code=303)
    return templates.TemplateResponse(request, "product_edit.html", {
        "staff": staff, "product": product,
        "category_labels": CATEGORY_LABELS, "cat_subcategories": CAT_SUBCATEGORIES,
    })


@app.post("/products/{product_id}/edit")
def product_edit(request: Request, product_id: str, name: str = Form(...), sku: str = Form(...),
                  category: str = Form(""), subcategory: str = Form(""), price: float = Form(0),
                  cost: float = Form(0), stock: int = Form(0), reorder_threshold: int = Form(5),
                  reorder_qty: int = Form(10), db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    product = db.get(Product, product_id)
    if product:
        product.name, product.sku = name, sku
        product.category, product.subcategory = category, subcategory
        product.price, product.cost, product.stock = price, cost, stock
        product.reorder_threshold, product.reorder_qty = reorder_threshold, reorder_qty
        add_audit(db, staff, "PRODUCT_EDIT", f"Edited product: {name}")
        db.commit()
    return RedirectResponse("/products", status_code=303)


@app.post("/products/{product_id}/delete")
def product_delete(request: Request, product_id: str, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden", status_code=403)
    product = db.get(Product, product_id)
    if product:
        add_audit(db, staff, "PRODUCT_DELETE", f"Deleted product: {product.name}")
        db.delete(product)
        db.commit()
    return RedirectResponse("/products", status_code=303)


# ── CUSTOMERS ────────────────────────────────────────────────────────
@app.get("/customers", response_class=HTMLResponse)
def customers_list(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    customers = db.query(Customer).order_by(Customer.name).all()
    return templates.TemplateResponse(request, "customers.html", {"staff": staff, "customers": customers})


@app.post("/customers/add")
def customer_add(request: Request, name: str = Form(...), phone: str = Form(""),
                  email: str = Form(""), notes: str = Form(""), db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    db.add(Customer(name=name, phone=phone, email=email, notes=notes))
    add_audit(db, staff, "CUSTOMER_ADD", f"Added customer: {name}")
    db.commit()
    return RedirectResponse("/customers", status_code=303)


@app.get("/customers/{customer_id}", response_class=HTMLResponse)
def customer_detail(request: Request, customer_id: str, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    customer = db.get(Customer, customer_id)
    invoices = db.query(Invoice).filter(Invoice.customer_id == customer_id).order_by(Invoice.date.desc()).all()
    return templates.TemplateResponse(request, "customer_detail.html", {
        "staff": staff, "customer": customer, "invoices": invoices,
    })


@app.post("/customers/{customer_id}/edit")
def customer_edit(request: Request, customer_id: str, name: str = Form(...),
                   phone: str = Form(""), email: str = Form(""), db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    customer = db.get(Customer, customer_id)
    if customer:
        customer.name, customer.phone, customer.email = name, phone, email
        add_audit(db, staff, "CUSTOMER_EDIT", f"Edited customer: {name}")
        db.commit()
    return RedirectResponse(f"/customers/{customer_id}", status_code=303)


@app.post("/customers/{customer_id}/notes")
def customer_notes(request: Request, customer_id: str, notes: str = Form(""), db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    customer = db.get(Customer, customer_id)
    if customer:
        customer.notes = notes
        add_audit(db, staff, "CUSTOMER_EDIT", f"Updated notes for {customer.name}")
        db.commit()
    return RedirectResponse(f"/customers/{customer_id}", status_code=303)


@app.post("/customers/{customer_id}/credit")
def customer_credit(request: Request, customer_id: str, amount: float = Form(...), db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — issuing store credit requires manager or owner role.", status_code=403)
    customer = db.get(Customer, customer_id)
    if customer:
        customer.store_credit = round((customer.store_credit or 0) + amount, 2)
        add_audit(db, staff, "STORE_CREDIT", f"Issued ${amount:.2f} store credit to {customer.name}")
        db.commit()
    return RedirectResponse(f"/customers/{customer_id}", status_code=303)


# ── REPAIRS ──────────────────────────────────────────────────────────
@app.get("/repairs", response_class=HTMLResponse)
def repairs_list(request: Request, view: str = "kanban", db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    repairs = db.query(Repair).order_by(Repair.created_at.desc()).all()
    columns = {s: [] for s in STATUS_ORDER}
    for r in repairs:
        columns.setdefault(r.status, []).append(r)
    technicians = db.query(Staff).filter(Staff.active == True).all()  # noqa: E712
    customers = db.query(Customer).order_by(Customer.name).all()
    return templates.TemplateResponse(request, "repairs.html", {
        "staff": staff, "repairs": repairs, "columns": columns, "view": view,
        "status_labels": STATUS_LABELS, "status_order": STATUS_ORDER, "status_badge": STATUS_BADGE,
        "issue_types": ISSUE_TYPES, "technicians": technicians, "customers": customers,
    })


@app.post("/repairs/add")
def repair_add(request: Request, phone: str = Form(...), name: str = Form(...),
                device: str = Form(...), issue: str = Form(...), description: str = Form(""),
                estimated_cost: str = Form(""), warranty_days: int = Form(90),
                promised_by: str = Form(""), technician_id: str = Form(""),
                db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)

    customer = db.query(Customer).filter(Customer.phone == phone).first()
    if not customer:
        customer = Customer(name=name, phone=phone)
        db.add(customer)
        db.flush()

    last = db.query(Repair).order_by(Repair.ticket_no.desc()).first()
    next_ticket = (last.ticket_no + 1) if last else 1001

    cost_val = float(estimated_cost) if estimated_cost else None
    history = [{"status": "RECEIVED", "note": "Ticket created", "date": datetime.utcnow().isoformat()}]

    repair = Repair(
        ticket_no=next_ticket, customer_id=customer.id, device=device, issue=issue,
        description=description, status="RECEIVED", estimated_cost=cost_val,
        warranty_days=warranty_days, promised_by=promised_by,
        technician_id=technician_id or None, status_history=json.dumps(history),
    )
    db.add(repair)
    add_audit(db, staff, "REPAIR_CREATE", f"Ticket #{next_ticket} — {device} ({issue})")
    db.commit()
    return RedirectResponse("/repairs", status_code=303)


@app.get("/repairs/{repair_id}", response_class=HTMLResponse)
def repair_detail(request: Request, repair_id: str, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    repair = db.get(Repair, repair_id)
    history = json.loads(repair.status_history) if repair.status_history else []
    n_status = next_status(repair.status)
    cur_idx = STATUS_ORDER.index(repair.status) if repair.status in STATUS_ORDER else 0
    technicians = db.query(Staff).filter(Staff.active == True).all()  # noqa: E712
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "repair_detail.html", {
        "staff": staff, "repair": repair, "history": list(reversed(history)),
        "next_status": n_status, "cur_idx": cur_idx,
        "status_labels": STATUS_LABELS, "status_order": STATUS_ORDER, "status_badge": STATUS_BADGE,
        "technicians": technicians, "flash": flash,
    })


@app.post("/repairs/{repair_id}/advance")
def repair_advance(request: Request, repair_id: str, note: str = Form(""), db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    repair = db.get(Repair, repair_id)
    if repair:
        n = next_status(repair.status)
        if n:
            old = repair.status
            repair.status = n
            repair.updated_at = datetime.utcnow()
            history = json.loads(repair.status_history) if repair.status_history else []
            history.append({"status": n, "note": note or f"Moved from {STATUS_LABELS.get(old, old)}", "date": datetime.utcnow().isoformat()})
            repair.status_history = json.dumps(history)
            add_audit(db, staff, "REPAIR_STATUS", f"#{repair.ticket_no} → {STATUS_LABELS.get(n, n)}")
            db.commit()
    return RedirectResponse(f"/repairs/{repair_id}", status_code=303)


@app.post("/repairs/{repair_id}/notify")
def repair_notify(request: Request, repair_id: str, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    repair = db.get(Repair, repair_id)
    if not repair or not repair.customer or not repair.customer.phone:
        request.session["flash"] = ("red", "No customer phone number on file for this ticket.")
        return RedirectResponse(f"/repairs/{repair_id}", status_code=303)

    shop_name = get_setting(db, "shop_name", "the shop")
    message = (f"Hi {repair.customer.name}, great news! Your {repair.device} repair is "
               f"complete and ready for pickup at {shop_name}. Ticket #{repair.ticket_no}. See you soon!")
    ok, msg = send_sms(db, repair.customer.phone, message, get_setting)
    if ok:
        add_audit(db, staff, "REPAIR_NOTIFY", f"Ready-for-pickup SMS sent for ticket #{repair.ticket_no}")
    request.session["flash"] = ("green" if ok else "red", msg)
    return RedirectResponse(f"/repairs/{repair_id}", status_code=303)


@app.post("/repairs/{repair_id}/cost")
def repair_cost(request: Request, repair_id: str, estimated_cost: str = Form(""),
                 final_cost: str = Form(""), db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    repair = db.get(Repair, repair_id)
    if repair:
        repair.estimated_cost = float(estimated_cost) if estimated_cost else repair.estimated_cost
        repair.final_cost = float(final_cost) if final_cost else repair.final_cost
        repair.updated_at = datetime.utcnow()
        add_audit(db, staff, "REPAIR_EDIT", f"Updated costs for ticket #{repair.ticket_no}")
        db.commit()
    return RedirectResponse(f"/repairs/{repair_id}", status_code=303)


# ── STAFF ────────────────────────────────────────────────────────────
@app.get("/staff", response_class=HTMLResponse)
def staff_list(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — staff management requires manager or owner role.", status_code=403)
    all_staff = db.query(Staff).all()
    return templates.TemplateResponse(request, "staff.html", {"staff": staff, "all_staff": all_staff})


@app.post("/staff/add")
def staff_add(request: Request, name: str = Form(...), pin: str = Form(...),
              role: str = Form("cashier"), db: Session = Depends(get_db)):
    current = require_login(request, db)
    if not current:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(current, "owner", "manager"):
        return HTMLResponse("Forbidden", status_code=403)
    if not pin.isdigit() or len(pin) != 4:
        return HTMLResponse("PIN must be exactly 4 digits", status_code=400)
    db.add(Staff(name=name, role=role, pin_hash=hash_pin(pin), active=True))
    add_audit(db, current, "STAFF_ADD", f"Added staff member: {name}")
    db.commit()
    return RedirectResponse("/staff", status_code=303)


@app.post("/staff/{staff_id}/edit")
def staff_edit(request: Request, staff_id: str, name: str = Form(...),
                role: str = Form(...), new_pin: str = Form(""), db: Session = Depends(get_db)):
    current = require_login(request, db)
    if not current:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(current, "owner", "manager"):
        return HTMLResponse("Forbidden", status_code=403)
    target = db.get(Staff, staff_id)
    if target:
        target.name, target.role = name, role
        if new_pin and new_pin.isdigit() and len(new_pin) == 4:
            target.pin_hash = hash_pin(new_pin)
        add_audit(db, current, "STAFF_EDIT", f"Edited staff member: {name}")
        db.commit()
    return RedirectResponse("/staff", status_code=303)


@app.post("/staff/{staff_id}/toggle")
def staff_toggle(request: Request, staff_id: str, db: Session = Depends(get_db)):
    current = require_login(request, db)
    if not current:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(current, "owner", "manager"):
        return HTMLResponse("Forbidden", status_code=403)
    target = db.get(Staff, staff_id)
    if target:
        target.active = not target.active
        add_audit(db, current, "STAFF_TOGGLE", f"{'Activated' if target.active else 'Deactivated'} {target.name}")
        db.commit()
    return RedirectResponse("/staff", status_code=303)


# ── SETTINGS ─────────────────────────────────────────────────────────
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner"):
        return HTMLResponse("Forbidden — settings require owner role.", status_code=403)
    settings = {s.key: s.value for s in db.query(Setting).all()}
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "settings.html", {"staff": staff, "settings": settings, "flash": flash})


@app.post("/settings")
def settings_save(request: Request, shop_name: str = Form(...), province: str = Form(...),
                   invoice_prefix: str = Form(...), shop_address: str = Form(""),
                   shop_phone: str = Form(""), shop_email: str = Form(""),
                   shop_gst: str = Form(""), shop_pst: str = Form(""),
                   points_per_dollar: float = Form(1), points_redeem_rate: float = Form(100),
                   smtp_host: str = Form(""), smtp_port: str = Form(""), smtp_user: str = Form(""),
                   smtp_password: str = Form(""), smtp_from: str = Form(""),
                   twilio_sid: str = Form(""), twilio_token: str = Form(""), twilio_from: str = Form(""),
                   digest_enabled: str = Form(""), digest_email: str = Form(""), digest_hour: int = Form(21),
                   db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner"):
        return HTMLResponse("Forbidden", status_code=403)
    set_setting(db, "shop_name", shop_name)
    set_setting(db, "province", province)
    set_setting(db, "invoice_prefix", invoice_prefix)
    set_setting(db, "shop_address", shop_address)
    set_setting(db, "shop_phone", shop_phone)
    set_setting(db, "shop_email", shop_email)
    set_setting(db, "shop_gst", shop_gst)
    set_setting(db, "shop_pst", shop_pst)
    set_setting(db, "points_per_dollar", str(points_per_dollar))
    set_setting(db, "points_redeem_rate", str(points_redeem_rate))
    set_setting(db, "smtp_host", smtp_host)
    set_setting(db, "smtp_port", smtp_port)
    set_setting(db, "smtp_user", smtp_user)
    if smtp_password:  # only overwrite if a new one was actually typed
        set_setting(db, "smtp_password", smtp_password)
    set_setting(db, "smtp_from", smtp_from)
    set_setting(db, "twilio_sid", twilio_sid)
    if twilio_token:
        set_setting(db, "twilio_token", twilio_token)
    set_setting(db, "twilio_from", twilio_from)
    set_setting(db, "digest_enabled", "true" if digest_enabled == "on" else "false")
    set_setting(db, "digest_email", digest_email)
    set_setting(db, "digest_hour", str(digest_hour))
    add_audit(db, staff, "SETTINGS", "Updated shop settings")
    _schedule_digest()  # pick up a new digest_hour immediately, not just on next restart
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/danger/wipe")
def settings_wipe_all(request: Request, confirm_text: str = Form(""), db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner"):
        return HTMLResponse("Forbidden — wiping data requires owner role.", status_code=403)
    if confirm_text.strip().lower() != "delete everything":
        return HTMLResponse('Type exactly "delete everything" to confirm.', status_code=400)

    for model in (InvoiceLine, Invoice, Repair, Customer, Product, HeldCart, CashSession):
        db.query(model).delete()
    add_audit(db, staff, "DANGER_WIPE", f"{staff.name} wiped all shop data (products/customers/repairs/invoices)")
    db.commit()
    return RedirectResponse("/settings", status_code=303)


# ── CASH SESSIONS ────────────────────────────────────────────────────
@app.get("/cashup", response_class=HTMLResponse)
def cashup_page(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)

    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    today_invoices = [i for i in db.query(Invoice).all()
                       if i.date.strftime("%Y-%m-%d") == today_str and not i.refunded]
    cash_sales = sum(i.total for i in today_invoices if i.payment_method == "Cash")
    card_sales = sum(i.total for i in today_invoices if i.payment_method in ("Credit Card", "Debit"))
    etransfer_sales = sum(i.total for i in today_invoices if i.payment_method == "E-Transfer")
    total_sales = sum(i.total for i in today_invoices)

    cash_float = float(get_setting(db, "cash_float", "200"))
    expected = round(cash_float + cash_sales, 2)
    today_session = db.query(CashSession).filter(CashSession.date == today_str).first()
    history = db.query(CashSession).order_by(CashSession.date.desc()).limit(10).all()

    return templates.TemplateResponse(request, "cashup.html", {
        "staff": staff, "total_sales": round(total_sales, 2), "cash_sales": round(cash_sales, 2),
        "card_sales": round(card_sales, 2), "etransfer_sales": round(etransfer_sales, 2),
        "invoice_count": len(today_invoices), "cash_float": cash_float, "expected": expected,
        "today_session": today_session, "history": history,
    })


@app.post("/cashup/close")
def cashup_close(request: Request, open_float: float = Form(...), actual: float = Form(...),
                  notes: str = Form(""), db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)

    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    today_invoices = [i for i in db.query(Invoice).all()
                       if i.date.strftime("%Y-%m-%d") == today_str and not i.refunded]
    cash_sales = sum(i.total for i in today_invoices if i.payment_method == "Cash")
    expected = round(open_float + cash_sales, 2)
    diff = round(actual - expected, 2)

    # Replace any existing session for today, same as the original
    existing = db.query(CashSession).filter(CashSession.date == today_str).first()
    if existing:
        db.delete(existing)
        db.flush()

    db.add(CashSession(
        date=today_str, open_float=open_float, expected=expected, actual=actual,
        difference=diff, notes=notes, closed_by_id=staff.id, closed_by_name=staff.name,
    ))
    set_setting(db, "cash_float", str(open_float))
    add_audit(db, staff, "CASH_UP", f"Cash-up closed: expected ${expected:.2f}, actual ${actual:.2f}, diff ${diff:.2f}")
    db.commit()
    return RedirectResponse("/cashup", status_code=303)


def build_daily_digest(db: Session) -> tuple[str, str]:
    """Returns (subject, body) for the daily owner digest email."""
    today = datetime.utcnow().date()
    today_str = today.strftime("%Y-%m-%d")
    shop_name = get_setting(db, "shop_name", "Your Shop")

    today_invoices = [i for i in db.query(Invoice).all() if i.date.date() == today]
    paid = [i for i in today_invoices if not i.refunded]
    refunded = [i for i in today_invoices if i.refunded]
    revenue = round(sum(i.total for i in paid), 2)
    tax = round(sum(i.tax_total for i in paid), 2)

    repairs_opened = db.query(Repair).filter(Repair.created_at >= datetime.combine(today, datetime.min.time())).count()
    repairs_closed_today = sum(1 for r in db.query(Repair).filter(Repair.status.in_(["COMPLETED", "COLLECTED"])).all()
                                if r.updated_at and r.updated_at.date() == today)

    low_stock = db.query(Product).filter(Product.stock <= Product.reorder_threshold).all()

    cash_session = db.query(CashSession).filter(CashSession.date == today_str).first()

    lines = [
        f"Daily summary for {shop_name} — {today.strftime('%A, %B')} {today.day}, {today.year}",
        "",
        f"💰 Revenue: ${revenue:.2f} ({len(paid)} sale{'s' if len(paid) != 1 else ''})",
        f"🧾 Tax collected: ${tax:.2f}",
    ]
    if refunded:
        lines.append(f"↩️ Refunds today: {len(refunded)} (${sum(i.total for i in refunded):.2f})")
    lines += [
        "",
        f"🔧 Repair tickets opened today: {repairs_opened}",
        f"✅ Repair tickets closed today: {repairs_closed_today}",
        "",
    ]
    if low_stock:
        lines.append(f"📦 Low stock — {len(low_stock)} item(s) need reordering:")
        for p in low_stock[:10]:
            lines.append(f"   • {p.name} ({p.sku}) — {p.stock} left, reorder {p.reorder_qty}")
        if len(low_stock) > 10:
            lines.append(f"   ...and {len(low_stock) - 10} more. Full list: /products/reorder")
    else:
        lines.append("📦 Stock levels are fine — nothing needs reordering.")
    lines.append("")
    if cash_session:
        diff = cash_session.difference
        lines.append(f"🔒 Cash-up was closed today — difference: {'+'if diff>=0 else ''}{diff:.2f}")
    else:
        lines.append("🔒 Cash-up has not been closed yet today.")

    return f"{shop_name} — Daily Summary ({today_str})", "\n".join(lines)


@app.post("/settings/send-test-email")
def send_test_email(request: Request, to_email: str = Form(...), db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner"):
        return HTMLResponse("Forbidden", status_code=403)
    shop_name = get_setting(db, "shop_name", "TechPro+")
    ok, msg = send_plain_email(
        db, to_email,
        f"Test email from {shop_name}",
        f"This is a test email from {shop_name}'s CRM. If you're reading this, SMTP is working correctly.",
        get_setting,
    )
    request.session["flash"] = ("green" if ok else "red", msg)
    return RedirectResponse("/settings", status_code=303)


@app.get("/settings/smtp-diagnose", response_class=HTMLResponse)
def smtp_diagnose_page(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner"):
        return HTMLResponse("Forbidden", status_code=403)
    return templates.TemplateResponse(request, "smtp_diagnose.html", {
        "staff": staff, "steps": None, "to_email": "",
        "settings": {s.key: s.value for s in db.query(Setting).all()},
    })


@app.post("/settings/smtp-diagnose", response_class=HTMLResponse)
def smtp_diagnose_run(request: Request, to_email: str = Form(...), db: Session = Depends(get_db)):
    """Runs SMTP connection step-by-step and reports exactly which phase
    fails — lets you see the precise error rather than a generic message."""
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner"):
        return HTMLResponse("Forbidden", status_code=403)

    import socket as _socket

    host = get_setting(db, "smtp_host", "")
    port_str = get_setting(db, "smtp_port", "587")
    user = get_setting(db, "smtp_user", "")
    password = get_setting(db, "smtp_password", "")
    from_addr = get_setting(db, "smtp_from", "") or user
    shop_name = get_setting(db, "shop_name", "TechPro+")

    steps = []

    def s_ok(label, detail=""): steps.append(("ok", label, detail))
    def s_fail(label, detail=""): steps.append(("fail", label, detail))
    def s_warn(label, detail=""): steps.append(("warn", label, detail))

    ctx = {"settings": {s.key: s.value for s in db.query(Setting).all()},
           "staff": staff, "to_email": to_email, "steps": steps}

    def render(): return templates.TemplateResponse(request, "smtp_diagnose.html", ctx)

    # Step 1: settings completeness
    if not (host and port_str and user and password):
        s_fail("Settings check", "SMTP host, port, username, or password is missing in Settings.")
        return render()
    s_ok("Settings check", f"Host: {host}, Port: {port_str}, User: {user}, From: {from_addr}")

    # Step 2: TCP connectivity
    port = int(port_str)
    try:
        sock = _socket.create_connection((host, port), timeout=10)
        sock.close()
        s_ok("TCP connection", f"Reached {host}:{port}")
    except Exception as e:
        s_fail("TCP connection", f"Cannot connect to {host}:{port} — {e}. Your ISP or Windows firewall may be blocking outbound port {port}.")
        return render()

    # Step 3: TLS handshake
    import smtplib, ssl as _ssl
    server = None
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=15, context=_ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            server.ehlo()
            server.starttls(context=_ssl.create_default_context())
            server.ehlo()
        s_ok("TLS / STARTTLS", f"Encryption established")
    except Exception as e:
        s_fail("TLS / STARTTLS", str(e))
        return render()

    # Step 4: SMTP login
    try:
        server.login(user, password)
        s_ok("SMTP authentication", f"Logged in as {user}")
    except smtplib.SMTPAuthenticationError as e:
        s_fail("SMTP authentication",
               f"Credentials rejected — {e}. "
               f"For Brevo: the password must be your SMTP Key "
               f"(Brevo dashboard → SMTP & API → SMTP → Generate a new SMTP key). "
               f"It is NOT your Brevo account password.")
        server.quit()
        return render()
    except Exception as e:
        s_fail("SMTP authentication", str(e))
        server.quit()
        return render()

    # Step 5: Brevo-specific verified sender check
    is_brevo = "brevo" in host.lower() or "sendinblue" in host.lower()
    if is_brevo:
        s_warn("Brevo verified sender check",
               f"Brevo requires the From address '{from_addr}' to be added and verified in your "
               f"Brevo account before mail will actually be delivered. "
               f"Even if this test passes, if '{from_addr}' is not a verified sender, "
               f"Brevo accepts it at the SMTP level but silently drops it. "
               f"→ Go to: app.brevo.com → Senders & IPs → Senders → Add a sender.")
    else:
        s_ok("From address", f"Sending as {from_addr}")

    # Step 6: actually send the test message
    from email.mime.text import MIMEText
    from email.utils import formatdate, make_msgid
    try:
        msg = MIMEText(
            f"SMTP diagnostic test from {shop_name} CRM.\n\n"
            f"Connection details:\n"
            f"  Server: {host}:{port}\n"
            f"  Auth user: {user}\n"
            f"  From address: {from_addr}\n"
            f"  To: {to_email}\n\n"
            f"If you are reading this, all 6 steps passed successfully.\n\n"
            f"If you are using Brevo and this message did NOT arrive:\n"
            f"  → The From address '{from_addr}' is not yet verified in your Brevo account.\n"
            f"  → Go to app.brevo.com → Senders & IPs → Senders → Add a sender.\n"
            f"  → Then try sending a receipt again."
        )
        msg["Subject"] = f"[SMTP Diagnostic] {shop_name} CRM — step-by-step test"
        msg["From"] = from_addr
        msg["To"] = to_email
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()
        refused = server.send_message(msg)
        server.quit()

        if refused:
            s_fail("Message accepted by server",
                   f"Server refused the recipient address: {refused}. "
                   f"Double-check the customer's email address.")
        else:
            s_ok("Message accepted by server",
                 f"✓ {host} accepted the message for delivery to {to_email}. "
                 f"Check inbox and spam folder. "
                 + ("If it doesn't arrive, the Brevo verified-sender issue above is most likely the cause."
                    if is_brevo else
                    "If it doesn't arrive within 2 minutes, check spam/junk."))
    except Exception as e:
        s_fail("Sending message", str(e))

    return render()


@app.post("/settings/send-test-digest")
def send_test_digest(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner"):
        return HTMLResponse("Forbidden", status_code=403)
    recipient = get_setting(db, "digest_email", "")
    if not recipient:
        request.session["flash"] = ("red", "Set a digest email address first.")
        return RedirectResponse("/settings", status_code=303)
    subject, body = build_daily_digest(db)
    ok, msg = send_plain_email(db, recipient, subject, body, get_setting)
    request.session["flash"] = ("green" if ok else "red", msg)
    return RedirectResponse("/settings", status_code=303)


def run_daily_digest_job():
    """Called by the scheduler — opens its own DB session since it runs
    outside any request."""
    db = SessionLocal()
    try:
        enabled = get_setting(db, "digest_enabled", "false") == "true"
        recipient = get_setting(db, "digest_email", "")
        if not (enabled and recipient):
            return
        subject, body = build_daily_digest(db)
        send_plain_email(db, recipient, subject, body, get_setting)
    finally:
        db.close()


_scheduler = BackgroundScheduler()
def _schedule_digest():
    db = SessionLocal()
    try:
        hour = int(get_setting(db, "digest_hour", "21"))
    finally:
        db.close()
    _scheduler.add_job(run_daily_digest_job, "cron", hour=hour, id="daily_digest", replace_existing=True)
_schedule_digest()
_scheduler.start()


# ── REPORTS ──────────────────────────────────────────────────────────
@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)

    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month_end = month_start - timedelta(seconds=1)
    last_month_start = last_month_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    all_invoices = db.query(Invoice).filter(Invoice.refunded == False).all()  # noqa: E712
    this_month = [i for i in all_invoices if i.date >= month_start]
    last_month = [i for i in all_invoices if last_month_start <= i.date <= last_month_end]

    month_revenue = round(sum(i.total for i in this_month), 2)
    last_month_revenue = round(sum(i.total for i in last_month), 2)
    month_tax = round(sum(i.tax_total for i in this_month), 2)
    revenue_trend_pct = round(((month_revenue - last_month_revenue) / last_month_revenue) * 100) if last_month_revenue else None

    # Profit: (price - product cost) * qty, across all (non-refunded) invoices
    profit = 0.0
    by_category = {}
    by_payment = {}
    prod_sales = {}
    for inv in all_invoices:
        by_payment[inv.payment_method] = by_payment.get(inv.payment_method, 0) + inv.total
        for line in inv.lines:
            product = db.get(Product, line.product_id) if line.product_id else None
            line_total = line.price * line.qty
            if product:
                profit += (line.price - product.cost) * line.qty
                cat = product.category or "Uncategorized"
                prod_sales.setdefault(line.product_id, {"name": line.name, "qty": 0, "rev": 0})
                prod_sales[line.product_id]["qty"] += line.qty
                prod_sales[line.product_id]["rev"] += line_total
            else:
                profit += line_total
                cat = "Repair / Service"
            by_category[cat] = by_category.get(cat, 0) + line_total

    top_products = sorted(prod_sales.values(), key=lambda p: p["qty"], reverse=True)[:8]

    return templates.TemplateResponse(request, "reports.html", {
        "staff": staff, "month_revenue": month_revenue, "last_month_revenue": last_month_revenue,
        "revenue_trend_pct": revenue_trend_pct, "month_tax": month_tax,
        "month_invoice_count": len(this_month), "profit": round(profit, 2),
        "by_category": sorted(by_category.items(), key=lambda x: x[1], reverse=True),
        "by_payment": sorted(by_payment.items(), key=lambda x: x[1], reverse=True),
        "top_products": top_products,
    })


def _build_eod_data(db: Session, month_str: str):
    """Shared by both the on-screen EOD report and the printable version."""
    year, month = int(month_str[:4]), int(month_str[5:7])
    invoices = (db.query(Invoice)
                .filter(Invoice.refunded == False)  # noqa: E712
                .order_by(Invoice.date).all())
    days = {}
    for inv in invoices:
        if inv.date.year != year or inv.date.month != month:
            continue
        key = inv.date.strftime("%Y-%m-%d")
        d = days.setdefault(key, {
            "date": key, "weekday": inv.date.strftime("%A"),
            "count": 0, "subtotal": 0.0, "tax": 0.0, "total": 0.0,
            "by_payment": {}, "invoices": [],
        })
        d["count"] += 1
        d["subtotal"] += inv.subtotal
        d["tax"] += inv.tax_total
        d["total"] += inv.total
        d["by_payment"][inv.payment_method] = d["by_payment"].get(inv.payment_method, 0) + inv.total
        d["invoices"].append(inv)

    day_list = sorted(days.values(), key=lambda d: d["date"], reverse=True)
    grand_revenue = round(sum(d["total"] for d in day_list), 2)
    grand_tax = round(sum(d["tax"] for d in day_list), 2)
    grand_count = sum(d["count"] for d in day_list)
    daily_avg = round(grand_revenue / len(day_list), 2) if day_list else 0
    return day_list, grand_revenue, grand_tax, grand_count, daily_avg


@app.get("/reports/eod", response_class=HTMLResponse)
def eod_report(request: Request, month: str = "", db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    month = month or datetime.utcnow().strftime("%Y-%m")
    day_list, grand_revenue, grand_tax, grand_count, daily_avg = _build_eod_data(db, month)
    return templates.TemplateResponse(request, "eod_report.html", {
        "staff": staff, "month": month, "days": day_list,
        "grand_revenue": grand_revenue, "grand_tax": grand_tax,
        "grand_count": grand_count, "daily_avg": daily_avg,
    })


@app.get("/reports/eod/print", response_class=HTMLResponse)
def eod_report_print(request: Request, month: str = "", day: str = "", db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    month = month or datetime.utcnow().strftime("%Y-%m")
    day_list, grand_revenue, grand_tax, grand_count, daily_avg = _build_eod_data(db, month)
    if day:
        day_list = [d for d in day_list if d["date"] == day]
        grand_revenue = round(sum(d["total"] for d in day_list), 2)
        grand_tax = round(sum(d["tax"] for d in day_list), 2)
        grand_count = sum(d["count"] for d in day_list)
    shop_name = get_setting(db, "shop_name", "TechPro+")
    return templates.TemplateResponse(request, "eod_print.html", {
        "staff": staff, "month": month, "single_day": day, "days": day_list,
        "grand_revenue": grand_revenue, "grand_tax": grand_tax,
        "grand_count": grand_count, "shop_name": shop_name,
    })


# ── DATA EXPORT / IMPORT / BACKUP ───────────────────────────────────
def _csv_response(rows, headers, filename):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerows(rows)
    return Response(content=buf.getvalue(), media_type="text/csv",
                     headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/export/backup")
def export_backup(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner"):
        return HTMLResponse("Forbidden — backups require owner role.", status_code=403)

    def row(obj, exclude=()):
        return {c.name: getattr(obj, c.name) for c in obj.__table__.columns if c.name not in exclude}

    data = {
        "version": 1, "exported_at": datetime.utcnow().isoformat(),
        "settings": {s.key: s.value for s in db.query(Setting).all()},
        "products": [row(p) for p in db.query(Product).all()],
        "customers": [row(c) for c in db.query(Customer).all()],
        "repairs": [row(r) for r in db.query(Repair).all()],
        "invoices": [dict(row(i), lines=[row(l) for l in i.lines]) for i in db.query(Invoice).all()],
        # Staff PIN hashes are intentionally excluded from the backup file —
        # a leaked backup shouldn't double as a leaked set of login credentials.
        # Staff records (names/roles) are kept; PINs must be re-set after a restore.
        "staff": [row(s, exclude=("pin_hash",)) for s in db.query(Staff).all()],
    }
    set_setting(db, "last_backup", datetime.utcnow().isoformat())
    add_audit(db, staff, "BACKUP_EXPORT", "Full data backup exported")
    filename = f"techpro_backup_{datetime.utcnow().strftime('%Y-%m-%d')}.json"
    return Response(content=json.dumps(data, indent=2, default=str), media_type="application/json",
                     headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.post("/import/backup")
async def import_backup(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner"):
        return HTMLResponse("Forbidden — restoring backups requires owner role.", status_code=403)

    try:
        data = json.loads(await file.read())
    except Exception as e:
        return HTMLResponse(f"Could not parse backup file: {e}", status_code=400)
    if not data.get("version"):
        return HTMLResponse("This doesn't look like a valid backup file.", status_code=400)

    if "settings" in data:
        for k, v in data["settings"].items():
            set_setting(db, k, v)
    if "products" in data:
        db.query(Product).delete()
        for p in data["products"]:
            db.add(Product(**p))  # keep original id so existing invoice line references stay valid
    if "customers" in data:
        db.query(Customer).delete()
        for c in data["customers"]:
            db.add(Customer(**c))
    if "repairs" in data:
        db.query(Repair).delete()
        for r in data["repairs"]:
            for dt_field in ("created_at", "updated_at"):
                if r.get(dt_field) and isinstance(r[dt_field], str):
                    try:
                        r[dt_field] = datetime.fromisoformat(r[dt_field])
                    except ValueError:
                        r[dt_field] = datetime.utcnow()
            db.add(Repair(**r))
    # Invoices/lines intentionally left alone on restore — overwriting sales
    # history is rarely what you want from a "restore my catalog/customers"
    # action. Flag this clearly to the person rather than silently doing it.

    add_audit(db, staff, "BACKUP_IMPORT", "Backup restored (products/customers/repairs/settings)")
    db.commit()
    return RedirectResponse("/settings", status_code=303)


@app.get("/export/csv/inventory")
def export_inventory_csv(request: Request, db: Session = Depends(get_db)):
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    rows = [(p.sku, p.name, p.category, p.subcategory, p.price, p.cost, p.stock) for p in db.query(Product).all()]
    return _csv_response(rows, ["SKU", "Name", "Category", "Brand", "Price", "Cost", "Stock"], "inventory.csv")


@app.get("/export/csv/customers")
def export_customers_csv(request: Request, db: Session = Depends(get_db)):
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    rows = [(c.name, c.phone, c.email, c.spent, c.points, c.store_credit) for c in db.query(Customer).all()]
    return _csv_response(rows, ["Name", "Phone", "Email", "Total Spent", "Points", "Store Credit"], "customers.csv")


@app.get("/export/csv/invoices")
def export_invoices_csv(request: Request, db: Session = Depends(get_db)):
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    rows = [(i.number, i.customer.name if i.customer else "Walk-in", i.payment_method,
             i.subtotal, i.discount, i.tax_total, i.total, i.date.strftime("%Y-%m-%d %H:%M"),
             "Refunded" if i.refunded else "Paid") for i in db.query(Invoice).order_by(Invoice.date).all()]
    return _csv_response(rows, ["Number", "Customer", "Payment", "Subtotal", "Discount", "Tax", "Total", "Date", "Status"], "invoices.csv")


@app.get("/export/csv/tax-report")
def export_tax_report_csv(request: Request, db: Session = Depends(get_db)):
    if not require_login(request, db):
        return RedirectResponse("/login", status_code=303)
    rows = [(i.number, i.date.strftime("%Y-%m-%d"), i.subtotal, i.discount, i.tax_total, i.total)
            for i in db.query(Invoice).filter(Invoice.refunded == False).order_by(Invoice.date).all()]  # noqa: E712
    return _csv_response(rows, ["Invoice", "Date", "Subtotal", "Discount", "Tax Collected", "Total"], "tax_report.csv")


# ── AUDIT LOG ────────────────────────────────────────────────────────
@app.get("/audit", response_class=HTMLResponse)
def audit_log(request: Request, db: Session = Depends(get_db)):
    staff = require_login(request, db)
    if not staff:
        return RedirectResponse("/login", status_code=303)
    if not role_allowed(staff, "owner", "manager"):
        return HTMLResponse("Forbidden — audit log requires manager or owner role.", status_code=403)
    logs = db.query(AuditLog).order_by(AuditLog.ts.desc()).limit(200).all()
    return templates.TemplateResponse(request, "audit.html", {"staff": staff, "logs": logs})
