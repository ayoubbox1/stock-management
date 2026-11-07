import os
import sys

# Ensure app can be imported
sys.path.insert(0, r"C:\Users\HP\.gemini\antigravity\scratch\stock_manager")

from app import app, db, User, Product, Category, Supplier, Tag, Customer, Invoice, PurchaseOrder, Return, Currency

def run_tests():
    print("=== Starting StockFlow Comprehensive Tests ===")
    with app.app_context():
        client = app.test_client()

        # 1. Test unauthenticated redirect to login
        res = client.get('/', follow_redirects=False)
        assert res.status_code == 302, f"Expected 302 redirect for unauthenticated user, got {res.status_code}"
        print("[PASS] Unauthenticated route redirects to /login")

        # 2. Test login page GET
        res = client.get('/login')
        assert res.status_code == 200, f"Expected 200 on /login, got {res.status_code}"
        print("[PASS] GET /login returns 200")

        # 3. Test login with admin
        res = client.post('/login', data={'username': 'admin', 'password': 'admin123'}, follow_redirects=True)
        assert res.status_code == 200
        assert b"Welcome back" in res.data or b"Dashboard" in res.data
        print("[PASS] Successfully logged in as admin")

        # 4. Verify all 10 required routes return 200
        required_routes = [
            '/',
            '/analytics',
            '/alerts',
            '/activity',
            '/search?q=test',
            '/purchase-orders',
            '/tags',
            '/returns',
            '/settings/currencies',
            '/users'
        ]

        for route in required_routes:
            res = client.get(route)
            assert res.status_code == 200, f"Route {route} failed with status {res.status_code}"
            print(f"[PASS] Route {route} returned 200 OK")

        # 5. Test Feature 7: Tag creation & listing
        res = client.post('/tag/new', data={'name': 'Bestseller', 'color': '#10b981'}, follow_redirects=True)
        assert res.status_code == 200
        tag = Tag.query.filter_by(name='Bestseller').first()
        assert tag is not None
        print(f"[PASS] Created Tag #{tag.name} (id={tag.id})")

        # 6. Test Category & Supplier creation
        res = client.post('/category/new', data={'name': 'Electronics', 'color': '#4f46e5', 'icon': 'bi-laptop'}, follow_redirects=True)
        cat = Category.query.filter_by(name='Electronics').first()
        assert cat is not None

        res = client.post('/supplier/new', data={'name': 'Global Tech Ltd', 'email': 'tech@global.com', 'phone': '12345'}, follow_redirects=True)
        sup = Supplier.query.filter_by(name='Global Tech Ltd').first()
        assert sup is not None
        print("[PASS] Created Category and Supplier")

        # 7. Test Product creation with Tag & Feature 2 (QR & Barcode)
        res = client.post('/product/new', data={
            'name': 'Ergonomic Keyboard',
            'sku': 'KB-990',
            'quantity': 25,
            'price': 89.99,
            'category_id': cat.id,
            'supplier_id': sup.id,
            'tag_ids': [tag.id]
        }, follow_redirects=True)
        prod = Product.query.filter_by(sku='KB-990').first()
        assert prod is not None
        assert tag in prod.tags
        print(f"[PASS] Created Product {prod.name} with Tag {tag.name}")

        # Test QR Code endpoint
        res = client.get(f'/product/{prod.id}/qrcode')
        assert res.status_code == 200
        assert res.mimetype == 'image/png'
        assert len(res.data) > 100
        print(f"[PASS] Product QR code generated ({len(res.data)} bytes, image/png)")

        # Test Barcode endpoint
        res = client.get(f'/product/{prod.id}/barcode')
        assert res.status_code == 200
        assert res.mimetype == 'image/png'
        assert len(res.data) > 100
        print(f"[PASS] Product Barcode generated ({len(res.data)} bytes, image/png)")

        # Test Tag filter on /
        res = client.get(f'/?tag={tag.id}')
        assert res.status_code == 200
        assert b"Ergonomic Keyboard" in res.data
        print("[PASS] Filter by tag works on product list")

        # 8. Test Feature 3: Purchase Order System
        res = client.post('/purchase-order/new', data={
            'supplier_id': sup.id,
            'notes': 'Restock shipment for Q1',
            'status': 'sent',
            'product_id': [prod.id],
            'quantity': [15],
            'unit_cost': [50.0]
        }, follow_redirects=True)
        assert res.status_code == 200
        po = PurchaseOrder.query.filter_by(supplier_id=sup.id).first()
        assert po is not None
        assert po.status == 'sent'
        assert po.total_cost == 750.0
        initial_stock = prod.quantity

        # Receive PO
        res = client.post(f'/purchase-order/{po.id}/receive', follow_redirects=True)
        assert res.status_code == 200
        db.session.refresh(prod)
        db.session.refresh(po)
        assert po.status == 'received'
        assert prod.quantity == initial_stock + 15
        print(f"[PASS] Purchase order created and received! Stock updated from {initial_stock} to {prod.quantity}")

        # 9. Test Feature 6: Multi-Currency & Invoices
        # Create Customer
        res = client.post('/customer/new', data={'name': 'Jane Doe', 'email': 'jane@example.com', 'company': 'Acme Corp'}, follow_redirects=True)
        cust = Customer.query.filter_by(name='Jane Doe').first()
        assert cust is not None

        # Get EUR currency
        eur = Currency.query.filter_by(code='EUR').first()
        assert eur is not None

        # Create Invoice in EUR
        stock_before_inv = prod.quantity
        res = client.post('/invoice/new', data={
            'customer_id': cust.id,
            'currency_id': eur.id,
            'discount_pct': 10.0,
            'product_id': [prod.id],
            'quantity': [5]
        }, follow_redirects=True)
        inv = Invoice.query.filter_by(customer_id=cust.id).first()
        assert inv is not None
        assert inv.currency_id == eur.id
        assert inv.currency_symbol == '€'
        db.session.refresh(prod)
        assert prod.quantity == stock_before_inv - 5
        print(f"[PASS] Created Invoice #INV-{inv.id:04d} in {eur.code} ({inv.currency_symbol}) - stock deducted to {prod.quantity}")

        # View invoice
        res = client.get(f'/invoice/{inv.id}')
        assert res.status_code == 200
        assert "€".encode('utf-8') in res.data or b"EUR" in res.data
        print("[PASS] Invoice view correctly renders currency symbol and amounts")

        # 10. Test Feature 8: Returns / Refunds
        stock_before_ret = prod.quantity
        inv_item = inv.items[0]
        res = client.post(f'/invoice/{inv.id}/return', data={
            'reason': 'Customer requested refund for 2 units',
            'item_id': [inv_item.id],
            'quantity': [2]
        }, follow_redirects=True)
        ret = Return.query.filter_by(invoice_id=inv.id).first()
        assert ret is not None
        assert ret.status == 'pending'
        print(f"[PASS] Return request #RET-{ret.id:04d} created (status: {ret.status}, amount: {ret.refund_amount})")

        # Approve Return
        res = client.post(f'/return/{ret.id}/approve', follow_redirects=True)
        assert res.status_code == 200
        db.session.refresh(ret)
        db.session.refresh(prod)
        assert ret.status == 'approved'
        assert prod.quantity == stock_before_ret + 2
        print(f"[PASS] Return approved and items restocked! Stock incremented to {prod.quantity}")

        # 11. Test Feature 4: Global Search
        res = client.get('/search?q=Ergonomic')
        assert res.status_code == 200
        assert b"Ergonomic Keyboard" in res.data
        res = client.get('/search?q=Global')
        assert res.status_code == 200
        assert b"Global Tech" in res.data
        res = client.get('/search?q=Jane')
        assert res.status_code == 200
        assert b"Jane Doe" in res.data
        print("[PASS] Global search finds products, suppliers, and customers")

        # 12. Test Feature 5: Activity Log
        res = client.get('/activity')
        assert res.status_code == 200
        assert b"Created product" in res.data
        assert b"Purchase order" in res.data or b"purchase order" in res.data
        print("[PASS] Activity log captures actions in timeline")

        # 13. Test User Management
        res = client.post('/register', data={
            'username': 'manager1',
            'email': 'manager@stockflow.local',
            'password': 'password123',
            'role': 'manager'
        }, follow_redirects=True)
        m_user = User.query.filter_by(username='manager1').first()
        assert m_user is not None
        assert m_user.role == 'manager'
        print("[PASS] Created new manager user")

        res = client.post(f'/user/{m_user.id}/edit', data={
            'role': 'admin',
            'is_active': '1'
        }, follow_redirects=True)
        db.session.refresh(m_user)
        assert m_user.role == 'admin'
        print("[PASS] Edited user role to admin")

        print("\n=== ALL TESTS PASSED SUCCESSFULLY! ALL 8 FEATURES VERIFIED. ===")

if __name__ == '__main__':
    run_tests()
