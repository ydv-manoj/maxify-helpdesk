"""Reset and seed the DB. Dates are relative to now so the 30-day window stays stable.

Run: python -m app.db.seed
"""
from datetime import datetime, timedelta

from app.db import models as m
from app.db.session import Base, SessionLocal, engine

PRODUCTS = [
    # sku, name, category, price, original_price, is_sale
    ("TEE-CRW-WHT", "Organic Cotton Crew Tee - White", "Tops", 39.00, None, False),
    ("SHT-LIN-SKY", "Linen Camp-Collar Shirt - Sky Blue", "Tops", 79.00, None, False),
    ("KNT-MER-GRY", "Merino Crewneck Sweater - Grey", "Knitwear", 109.00, None, False),
    ("JNS-SLV-IND", "Slim Selvedge Jeans - Indigo", "Bottoms", 129.00, None, False),
    ("CT-WOL-CML", "Double-Faced Wool Coat - Camel", "Outerwear", 420.00, None, False),
    ("BT-CHL-BRN", "Leather Chelsea Boots - Brown", "Footwear", 189.00, None, False),
    ("SNK-CNV-WHT", "Canvas Low-Top Sneakers - White", "Footwear", 89.00, None, False),
    ("BAG-TOT-NAT", "Heavy Canvas Tote - Natural", "Accessories", 45.00, None, False),
    ("JKT-DNM-BLK", "Cropped Denim Jacket - Washed Black", "Outerwear", 69.00, 139.00, True),
    ("DRS-WRP-OLV", "Midi Wrap Dress - Olive", "Dresses", 59.00, 98.00, True),
    ("SHR-CHN-KHK", "Chino Shorts - Khaki", "Bottoms", 29.00, 55.00, True),
    ("SCF-SLK-NVY", "Silk Twill Scarf - Navy", "Accessories", 35.00, 70.00, True),
]

CUSTOMERS = [
    ("CUST-001", "Aisha Tan", "aisha.tan@example.sg"),        # Customer A: one order in transit
    ("CUST-002", "Marcus Lim", "marcus.lim@example.sg"),      # Customer B: order A must NOT see
    ("CUST-003", "Priya Nair", "priya.nair@example.sg"),      # 3 orders -> "return my order" is ambiguous
    ("CUST-004", "Daniel Wong", "daniel.wong@example.sg"),    # expensive order -> S$400 refund needs approval
    ("CUST-005", "Mei Ling Chua", "meiling.chua@example.sg"), # defective sale item -> refund allowed
    ("CUST-006", "Rahul Menon", "rahul.menon@example.sg"),    # sale item, changed mind -> exchange only
    ("CUST-007", "Sarah Koh", "sarah.koh@example.sg"),        # delivered 45 days ago -> outside window
    ("CUST-008", "Manoj Yadav (test user)", "manoj.test@example.sg"),  # live-chat test user: one order per policy branch
]

# order_id, customer_id (None = guest), email, status, placed_days_ago, shipped_days_ago, delivered_days_ago, items[(sku, qty)]
ORDERS = [
    ("ORD-10021", "CUST-001", None, "shipped", 3, 2, None, [("SHT-LIN-SKY", 1), ("TEE-CRW-WHT", 2)]),
    ("ORD-10022", "CUST-002", None, "delivered", 15, 13, 12, [("JNS-SLV-IND", 1), ("SNK-CNV-WHT", 1)]),
    ("ORD-10031", "CUST-003", None, "delivered", 14, 12, 10, [("KNT-MER-GRY", 1)]),
    ("ORD-10032", "CUST-003", None, "delivered", 7, 6, 4, [("BAG-TOT-NAT", 1), ("TEE-CRW-WHT", 1)]),
    ("ORD-10033", "CUST-003", None, "processing", 0, None, None, [("SNK-CNV-WHT", 1)]),
    ("ORD-10041", "CUST-004", None, "delivered", 10, 9, 7, [("CT-WOL-CML", 1), ("BT-CHL-BRN", 1)]),
    ("ORD-10051", "CUST-005", None, "delivered", 9, 7, 5, [("JKT-DNM-BLK", 1)]),
    ("ORD-10061", "CUST-006", None, "delivered", 11, 10, 8, [("DRS-WRP-OLV", 1)]),
    ("ORD-10071", "CUST-007", None, "delivered", 50, 48, 45, [("BT-CHL-BRN", 1)]),
    # live test user: shipped / delivered >S$200 / sale items / out of window / processing
    ("ORD-10091", "CUST-008", None, "shipped", 2, 1, None, [("SHT-LIN-SKY", 1), ("BAG-TOT-NAT", 1)]),
    ("ORD-10092", "CUST-008", None, "delivered", 9, 8, 6, [("KNT-MER-GRY", 1), ("JNS-SLV-IND", 1)]),
    ("ORD-10093", "CUST-008", None, "delivered", 15, 14, 12, [("DRS-WRP-OLV", 1), ("SCF-SLK-NVY", 1)]),
    ("ORD-10094", "CUST-008", None, "delivered", 44, 42, 40, [("SNK-CNV-WHT", 1)]),
    ("ORD-10095", "CUST-008", None, "processing", 0, None, None, [("BT-CHL-BRN", 1)]),
    ("ORD-10081", None, "jun.park@example.sg", "delivered", 6, 5, 3, [("KNT-MER-GRY", 1), ("SCF-SLK-NVY", 1)]),
]

POLICIES = [
    ("returns", "Returns & refunds",
     "Items can be returned for a full refund within 30 days of delivery, unworn with tags attached. "
     "Refunds go back to the original payment method within 5-7 working days of approval."),
    ("sale_items", "Sale items",
     "Sale items are exchange-only (different size or colour, subject to stock) within 30 days of delivery. "
     "Exception: a defective sale item can be refunded."),
    ("defective", "Defective items",
     "If an item arrives faulty or fails under normal use within 30 days of delivery, we offer a refund or "
     "replacement. Our team may ask for a photo. Defect refunds are reviewed by a team member before payout."),
    ("cancellation", "Cancellations",
     "Orders can be cancelled free of charge while they are still processing. Once shipped, please use a return."),
    ("refund_approval", "Large refunds",
     "Refunds above S$200 are reviewed and approved by a team member, usually within one working day."),
    ("shipping", "Shipping",
     "Standard delivery in Singapore takes 2-4 working days via Ninja Van. Free shipping on orders over S$80."),
]


def seed() -> None:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    now = datetime.now().replace(microsecond=0)
    ago = lambda d: None if d is None else now - timedelta(days=d)  # noqa: E731
    with SessionLocal() as db:
        prods = {}
        for sku, name, cat, price, orig, sale in PRODUCTS:
            prods[sku] = m.Product(sku=sku, name=name, category=cat, price=price, original_price=orig, is_sale=sale)
            db.add(prods[sku])
        emails = {}
        for cid, name, email in CUSTOMERS:
            db.add(m.Customer(id=cid, name=name, email=email))
            emails[cid] = email
        for oid, cid, guest_email, status, placed, shipped, delivered, items in ORDERS:
            order_items = [m.OrderItem(sku=s, name=prods[s].name, qty=q, unit_price=prods[s].price,
                                       was_on_sale=prods[s].is_sale) for s, q in items]
            db.add(m.Order(
                id=oid, customer_id=cid, email=emails.get(cid, guest_email), status=status,
                placed_at=ago(placed), shipped_at=ago(shipped), delivered_at=ago(delivered),
                carrier="Ninja Van" if shipped is not None else None,
                tracking_no=f"NV-SG-{oid[-5:]}88" if shipped is not None else None,
                total=round(sum(i.unit_price * i.qty for i in order_items), 2), items=order_items))
        for key, title, text in POLICIES:
            db.add(m.Policy(key=key, title=title, text=text))
        db.commit()


if __name__ == "__main__":
    seed()
    with SessionLocal() as db:
        for o in db.query(m.Order).order_by(m.Order.id):
            who = o.customer.name if o.customer else f"guest <{o.email}>"
            items = ", ".join(f"{i.name}{' (SALE)' if i.was_on_sale else ''}" for i in o.items)
            print(f"{o.id}  {who:<26} {o.status:<10} S${o.total:>7.2f}  {items}")
