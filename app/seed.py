from sqlalchemy import inspect, text
from .database import Base, engine, SessionLocal
from .models import Staff, Product, Setting
from .auth import hash_pin

# Sensible defaults for columns that might get added to existing tables
# later — used only when a column doesn't exist yet in an older crm.db.
_COLUMN_DEFAULTS = {
    "subcategory": "''", "reorder_threshold": "5", "reorder_qty": "10",
    "loyalty_pts_used": "0", "store_credit_used": "0", "tendered": "0", "change_given": "0",
    "sku": "''", "refunded": "0",
}


def migrate_schema():
    """SQLAlchemy's create_all() only creates NEW tables — it never adds
    columns to tables that already exist. Since this app has gone through
    several rounds of new fields (Product.subcategory, Invoice.tendered,
    etc.), an older crm.db file would be missing those columns entirely,
    which surfaces as a 500 error the moment any query touches them. This
    inspects the live database and adds whatever's missing, so upgrading
    the app code doesn't require deleting your data.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # brand-new table — create_all() already handles this
            existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing_cols:
                    continue
                default = _COLUMN_DEFAULTS.get(col.name, "NULL")
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} DEFAULT {default}'))


def init_db():
    Base.metadata.create_all(bind=engine)
    migrate_schema()
    db = SessionLocal()
    try:
        if not db.query(Staff).first():
            db.add(Staff(id="s1", name="Owner", role="owner", pin_hash=hash_pin("1234"), active=True))
        existing_name = db.query(Setting).filter_by(key="shop_name").first()
        if not existing_name:
            db.add(Setting(key="shop_name", value="TechPro+"))
        elif existing_name.value == "TechPro Repairs":
            # Migrate old default name to the new brand name
            existing_name.value = "TechPro+"
        if not db.query(Setting).filter_by(key="province").first():
            db.add(Setting(key="province", value="ON"))
        if not db.query(Setting).filter_by(key="invoice_prefix").first():
            db.add(Setting(key="invoice_prefix", value="INV"))
        if not db.query(Setting).filter_by(key="invoice_counter").first():
            db.add(Setting(key="invoice_counter", value="1000"))
        if not db.query(Setting).filter_by(key="points_per_dollar").first():
            db.add(Setting(key="points_per_dollar", value="1"))
        if not db.query(Setting).filter_by(key="points_redeem_rate").first():
            db.add(Setting(key="points_redeem_rate", value="100"))  # 100 points = $1
        if not db.query(Setting).filter_by(key="cash_float").first():
            db.add(Setting(key="cash_float", value="200"))
        if not db.query(Setting).filter_by(key="shop_gst").first():
            db.add(Setting(key="shop_gst", value=""))
        if not db.query(Setting).filter_by(key="shop_pst").first():
            db.add(Setting(key="shop_pst", value=""))
        if not db.query(Setting).filter_by(key="digest_enabled").first():
            db.add(Setting(key="digest_enabled", value="false"))
        if not db.query(Setting).filter_by(key="digest_email").first():
            db.add(Setting(key="digest_email", value=""))
        if not db.query(Setting).filter_by(key="digest_hour").first():
            db.add(Setting(key="digest_hour", value="21"))  # 24h, server-local time
        if not db.query(Product).first():
            db.add_all([
                Product(sku="SCRN-IP13", name="iPhone 13 Screen Protector", category="ACCESSORY", subcategory="Screen Protector", price=14.99, cost=3.50, stock=40),
                Product(sku="CASE-IP13", name="iPhone 13 Case", category="CASE", subcategory="OtterBox", price=19.99, cost=5.00, stock=25),
                Product(sku="CHRG-USBC", name="USB-C Fast Charger", category="CHARGER", subcategory="Anker", price=24.99, cost=8.00, stock=30),
                Product(sku="BATT-IP12", name="iPhone 12 Replacement Battery", category="BATTERY", subcategory="OEM Grade", price=49.99, cost=18.00, stock=10),
            ])
        db.commit()
    finally:
        db.close()
