import os
import uuid
import csv
import io
import json
import atexit
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, Blueprint, render_template, request, redirect,
                   url_for, flash, send_file, abort, Response, jsonify)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin, login_user,
                         logout_user, login_required, current_user)
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from io import BytesIO

import qrcode
import barcode
from barcode.writer import ImageWriter

from apscheduler.schedulers.background import BackgroundScheduler


# ── App setup ──────────────────────────────────────────────────
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'stockflow-secret-key-change-in-production-2026')
basedir = os.path.abspath(os.path.dirname(__file__))

# Database URI: allows connecting external Postgres/MySQL (e.g. Supabase, Neon) or local SQLite
database_url = os.environ.get('DATABASE_URL')
if database_url:
    # Fix potential postgres:// to postgresql:// for SQLAlchemy compatibility
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
elif os.environ.get('VERCEL'):
    # In Vercel serverless environments without an external DB, use /tmp/
    app.config['SQLALCHEMY_DATABASE_URI'] = "sqlite:////tmp/inventory.db"
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{os.path.join(basedir, 'inventory.db')}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join(basedir, 'static', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5 MB max

# Email configuration
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.environ.get('MAIL_USE_TLS', 'true').lower() in ('true', '1', 't')
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', '')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD', '')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER', 'noreply@stockflow.com')

API_KEY = os.environ.get('API_KEY', 'stockflow-api-key-change-me')

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

db = SQLAlchemy(app)
mail = Mail(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message_category = 'info'


# ── Role-Based Permissions Matrix ──────────────────────────────
PERMISSIONS = {
    'admin': {'all'},
    'manager': {
        'view_all', 'create', 'edit', 'delete_product', 'delete_customer',
        'export', 'analytics', 'po_manage', 'returns_manage', 'warehouse_manage'
    },
    'staff': {'view_all', 'create', 'edit', 'export'}
}

PERM_DEFINITIONS = [
    ('view_all', 'View All Records', 'Browse inventory, customers, invoices, suppliers and warehouses.'),
    ('create', 'Create Entities', 'Create new products, customers, suppliers and draft invoices.'),
    ('edit', 'Edit Records', 'Modify product details, customer profiles and supplier contacts.'),
    ('delete_product', 'Delete Products', 'Permanently remove products and associated media.'),
    ('delete_customer', 'Delete Customers', 'Permanently remove customer records.'),
    ('delete_supplier', 'Delete Suppliers', 'Remove supplier records (Admin/Manager).'),
    ('export', 'Data Export', 'Export CSV datasets for products, invoices, and customers.'),
    ('analytics', 'Business Analytics', 'Access executive revenue metrics, charts, and customer trends.'),
    ('po_manage', 'Purchase Orders', 'Authorize purchase orders and restock inventory on receipt.'),
    ('returns_manage', 'Returns & Refunds', 'Approve or reject customer return requests and restock units.'),
    ('warehouse_manage', 'Warehouse Operations', 'Transfer stock between warehouses and adjust facility levels.'),
    ('all', 'System Administration', 'Manage user accounts, assign roles, inspect audit logs, and configure SMTP & crons.')
]


def has_permission(perm):
    if not current_user or not current_user.is_authenticated:
        return False
    role = getattr(current_user, 'role', 'staff')
    role_perms = PERMISSIONS.get(role, set())
    return 'all' in role_perms or perm in role_perms


def require_permission(perm):
    def decorator(f):
        @wraps(f)
        @login_required
        def decorated_function(*args, **kwargs):
            if not has_permission(perm):
                abort(403)
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def admin_required(f):
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            abort(403)
        return f(*args, **kwargs)
    return decorated_function


@app.errorhandler(403)
def access_denied(e):
    return render_template('403.html'), 403


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def save_upload(file):
    if file and file.filename and allowed_file(file.filename):
        ext = file.filename.rsplit('.', 1)[1].lower()
        filename = f"{uuid.uuid4().hex}.{ext}"
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        return filename
    return None


# ── Association Tables ─────────────────────────────────────────
product_tags = db.Table('product_tags',
    db.Column('product_id', db.Integer, db.ForeignKey('product.id'), primary_key=True),
    db.Column('tag_id', db.Integer, db.ForeignKey('tag.id'), primary_key=True)
)


# ── Models ─────────────────────────────────────────────────────
class User(db.Model, UserMixin):
    __tablename__ = "user"
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80), unique=True, nullable=False)
    email         = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role          = db.Column(db.String(20), default='staff')
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    is_active     = db.Column(db.Boolean, default=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def initials(self):
        parts = self.username.strip().split()
        if len(parts) >= 2:
            return (parts[0][0] + parts[1][0]).upper()
        return self.username[:2].upper() if self.username else '?'

    def __repr__(self):
        return f"<User {self.username} ({self.role})>"


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


class Currency(db.Model):
    __tablename__ = "currency"
    id            = db.Column(db.Integer, primary_key=True)
    code          = db.Column(db.String(10), unique=True, nullable=False)
    symbol        = db.Column(db.String(10), nullable=False)
    name          = db.Column(db.String(50), nullable=False)
    exchange_rate = db.Column(db.Float, default=1.0)

    def __repr__(self):
        return f"<Currency {self.code} ({self.symbol})>"


class Tag(db.Model):
    __tablename__ = "tag"
    id    = db.Column(db.Integer, primary_key=True)
    name  = db.Column(db.String(50), unique=True, nullable=False)
    color = db.Column(db.String(20), default='#6366f1')

    def __repr__(self):
        return f"<Tag {self.name}>"


class Category(db.Model):
    __tablename__ = "category"
    id       = db.Column(db.Integer, primary_key=True)
    name     = db.Column(db.String(100), nullable=False)
    color    = db.Column(db.String(20), default='#4f46e5')
    icon     = db.Column(db.String(50), default='bi-tag')
    products = db.relationship('Product', backref='category', lazy=True)

    def __repr__(self):
        return f"<Category {self.name}>"


class Supplier(db.Model):
    __tablename__ = "supplier"
    id              = db.Column(db.Integer, primary_key=True)
    name            = db.Column(db.String(120), nullable=False)
    email           = db.Column(db.String(120), nullable=True)
    phone           = db.Column(db.String(40),  nullable=True)
    address         = db.Column(db.Text, nullable=True)
    notes           = db.Column(db.Text, nullable=True)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    products        = db.relationship('Product', backref='supplier', lazy=True)
    purchase_orders = db.relationship('PurchaseOrder', backref='supplier', lazy=True, cascade="all, delete-orphan")

    @property
    def initials(self):
        parts = self.name.strip().split()
        return ''.join(p[0].upper() for p in parts[:2]) if parts else '?'

    def __repr__(self):
        return f"<Supplier {self.name}>"


class Warehouse(db.Model):
    __tablename__ = "warehouse"
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(120), nullable=False)
    location      = db.Column(db.String(200), nullable=True)
    notes         = db.Column(db.Text, nullable=True)
    is_default    = db.Column(db.Boolean, default=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    stock_entries = db.relationship('WarehouseStock', backref='warehouse', lazy=True, cascade="all, delete-orphan")

    @property
    def total_skus(self):
        return sum(1 for entry in self.stock_entries if entry.quantity > 0)

    @property
    def total_units(self):
        return sum(entry.quantity for entry in self.stock_entries)

    def __repr__(self):
        return f"<Warehouse {self.name}>"


class WarehouseStock(db.Model):
    __tablename__ = "warehouse_stock"
    id           = db.Column(db.Integer, primary_key=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=False)
    product_id   = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    quantity     = db.Column(db.Integer, default=0, nullable=False)
    __table_args__ = (db.UniqueConstraint('warehouse_id', 'product_id', name='_wh_prod_uc'),)

    product = db.relationship('Product', backref=db.backref('warehouse_stocks', lazy=True, cascade="all, delete-orphan"))

    def __repr__(self):
        return f"<WarehouseStock WH:{self.warehouse_id} P:{self.product_id} Qty:{self.quantity}>"


class StockTransfer(db.Model):
    __tablename__ = "stock_transfer"
    id                = db.Column(db.Integer, primary_key=True)
    from_warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=False)
    to_warehouse_id   = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=False)
    product_id        = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    quantity          = db.Column(db.Integer, nullable=False)
    user_id           = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    created_at        = db.Column(db.DateTime, default=datetime.utcnow)

    from_warehouse = db.relationship('Warehouse', foreign_keys=[from_warehouse_id])
    to_warehouse   = db.relationship('Warehouse', foreign_keys=[to_warehouse_id])
    product        = db.relationship('Product')
    user           = db.relationship('User')

    def __repr__(self):
        return f"<StockTransfer {self.product.name if self.product else self.product_id}: {self.quantity} units>"


class ScheduleConfig(db.Model):
    __tablename__ = "schedule_config"
    id          = db.Column(db.Integer, primary_key=True)
    job_id      = db.Column(db.String(80), unique=True, nullable=False)
    enabled     = db.Column(db.Boolean, default=True)
    last_run    = db.Column(db.DateTime, nullable=True)
    last_status = db.Column(db.String(120), default='Scheduled')

    def __repr__(self):
        return f"<ScheduleConfig {self.job_id} Enabled:{self.enabled}>"


class Product(db.Model):
    __tablename__ = "product"
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(120), nullable=False)
    sku         = db.Column(db.String(80),  unique=True, nullable=False)
    quantity    = db.Column(db.Integer, default=0)
    price       = db.Column(db.Float, nullable=False)
    image       = db.Column(db.String(200), nullable=True)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'), nullable=True)
    tags        = db.relationship('Tag', secondary=product_tags, backref=db.backref('products', lazy='dynamic'))

    @property
    def image_url(self):
        if self.image:
            return url_for('static', filename=f'uploads/{self.image}')
        return None

    def __repr__(self):
        return f"<Product {self.name} ({self.sku})>"


class Customer(db.Model):
    __tablename__ = "customer"
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(120), nullable=False)
    email      = db.Column(db.String(120), nullable=True)
    phone      = db.Column(db.String(40),  nullable=True)
    company    = db.Column(db.String(120), nullable=True)
    address    = db.Column(db.Text, nullable=True)
    notes      = db.Column(db.Text, nullable=True)
    status     = db.Column(db.String(20), default='lead')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    invoices   = db.relationship('Invoice', backref='customer', lazy=True)

    @property
    def total_spent(self):
        return sum(inv.total_amount for inv in self.invoices)

    @property
    def initials(self):
        parts = self.name.strip().split()
        return ''.join(p[0].upper() for p in parts[:2]) if parts else '?'

    def __repr__(self):
        return f"<Customer {self.name}>"


class Invoice(db.Model):
    __tablename__ = "invoice"
    id           = db.Column(db.Integer, primary_key=True)
    client_name  = db.Column(db.String(120), nullable=False)
    customer_id  = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=True)
    currency_id  = db.Column(db.Integer, db.ForeignKey('currency.id'), nullable=True)
    discount_pct = db.Column(db.Float, default=0.0)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    currency     = db.relationship('Currency')
    items        = db.relationship('InvoiceItem', backref='invoice', lazy=True, cascade="all, delete-orphan")
    returns      = db.relationship('Return', backref='invoice', lazy=True, cascade="all, delete-orphan")

    @property
    def currency_symbol(self):
        return self.currency.symbol if self.currency else '$'

    @property
    def currency_code(self):
        return self.currency.code if self.currency else 'USD'

    @property
    def subtotal(self):
        return sum(item.total_price for item in self.items)

    @property
    def discount_amount(self):
        return round(self.subtotal * ((self.discount_pct or 0.0) / 100.0), 2)

    @property
    def total_amount(self):
        return round(self.subtotal * (1.0 - ((self.discount_pct or 0.0) / 100.0)), 2)

    def __repr__(self):
        return f"<Invoice {self.id} for {self.client_name}>"


class InvoiceItem(db.Model):
    __tablename__ = "invoice_item"
    id         = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey('invoice.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    quantity   = db.Column(db.Integer, nullable=False, default=1)
    unit_price = db.Column(db.Float, nullable=False)
    product    = db.relationship('Product')

    @property
    def total_price(self):
        return self.quantity * self.unit_price

    def __repr__(self):
        return f"<InvoiceItem {self.product.name if self.product else self.product_id} x{self.quantity}>"


class PurchaseOrder(db.Model):
    __tablename__ = "purchase_order"
    id          = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'), nullable=False)
    status      = db.Column(db.String(20), default='draft')
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    notes       = db.Column(db.Text, nullable=True)
    items       = db.relationship('POItem', backref='purchase_order', lazy=True, cascade="all, delete-orphan")

    @property
    def total_cost(self):
        return sum(item.total_cost for item in self.items)

    @property
    def items_count(self):
        return sum(item.quantity for item in self.items)

    def __repr__(self):
        return f"<PurchaseOrder PO-{self.id:04d} ({self.status})>"


class POItem(db.Model):
    __tablename__ = "po_item"
    id         = db.Column(db.Integer, primary_key=True)
    po_id      = db.Column(db.Integer, db.ForeignKey('purchase_order.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    quantity   = db.Column(db.Integer, nullable=False, default=1)
    unit_cost  = db.Column(db.Float, nullable=False, default=0.0)
    product    = db.relationship('Product')

    @property
    def total_cost(self):
        return self.quantity * self.unit_cost

    def __repr__(self):
        return f"<POItem {self.product.name if self.product else self.product_id} x{self.quantity}>"


class Return(db.Model):
    __tablename__ = "return"
    id            = db.Column(db.Integer, primary_key=True)
    invoice_id    = db.Column(db.Integer, db.ForeignKey('invoice.id'), nullable=False)
    reason        = db.Column(db.Text, nullable=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    refund_amount = db.Column(db.Float, default=0.0)
    status        = db.Column(db.String(20), default='pending')
    items         = db.relationship('ReturnItem', backref='return_record', lazy=True, cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Return {self.id} for Invoice {self.invoice_id} ({self.status})>"


class ReturnItem(db.Model):
    __tablename__ = "return_item"
    id         = db.Column(db.Integer, primary_key=True)
    return_id  = db.Column(db.Integer, db.ForeignKey('return.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    quantity   = db.Column(db.Integer, nullable=False, default=1)
    unit_price = db.Column(db.Float, nullable=False, default=0.0)
    product    = db.relationship('Product')

    @property
    def total_price(self):
        return self.quantity * self.unit_price

    def __repr__(self):
        return f"<ReturnItem {self.product.name if self.product else self.product_id} x{self.quantity}>"


class ActivityLog(db.Model):
    __tablename__ = "activity_log"
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    user        = db.relationship('User')
    action      = db.Column(db.String(120), nullable=False)
    entity_type = db.Column(db.String(50), nullable=True)
    entity_id   = db.Column(db.Integer, nullable=True)
    entity_name = db.Column(db.String(150), nullable=True)
    timestamp   = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<ActivityLog {self.action} on {self.entity_type} {self.entity_name}>"


# ── Activity Logger Helper ────────────────────────────────────
def log_activity(action, entity_type=None, entity_id=None, entity_name=None):
    try:
        user_id = current_user.id if current_user and current_user.is_authenticated else None
        log = ActivityLog(
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=str(entity_name) if entity_name else None
        )
        db.session.add(log)
        db.session.commit()
    except Exception:
        db.session.rollback()


# ── Warehouse Stock Helpers ────────────────────────────────────
def get_default_warehouse():
    wh = Warehouse.query.filter_by(is_default=True).first()
    if not wh:
        wh = Warehouse.query.first()
    if not wh:
        wh = Warehouse(name="Main Warehouse", location="Primary Facility", is_default=True)
        db.session.add(wh)
        db.session.commit()
    return wh


def sync_product_quantity(product_id):
    prod = Product.query.get(product_id)
    if prod:
        total = db.session.query(db.func.sum(WarehouseStock.quantity)).filter(WarehouseStock.product_id == product_id).scalar() or 0
        prod.quantity = total
        db.session.commit()


# ── Context Processors ─────────────────────────────────────────
@app.context_processor
def inject_global_data():
    try:
        low_stock_count = Product.query.filter(Product.quantity <= 10).count()
    except Exception:
        low_stock_count = 0
    return dict(
        low_stock_count=low_stock_count,
        permissions=PERMISSIONS,
        perm_definitions=PERM_DEFINITIONS
    )


# ── Email Helper Functions ─────────────────────────────────────
def send_email_safe(subject, recipients, html_body):
    if not recipients:
        return False, "No recipient specified."
    if isinstance(recipients, str):
        recipients = [recipients]
    recipients = [r.strip() for r in recipients if r and r.strip()]
    if not recipients:
        return False, "Empty recipient list."

    try:
        msg = Message(
            subject=subject,
            recipients=recipients,
            html=html_body,
            sender=app.config.get('MAIL_DEFAULT_SENDER')
        )
        mail.send(msg)
        return True, "Email dispatched successfully."
    except Exception as e:
        return False, f"Email delivery error: {str(e)}"


# ── REST API Blueprint (Feature 2) ─────────────────────────────
api_bp = Blueprint('api_v1', __name__, url_prefix='/api/v1')


def require_api_key(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        key = request.headers.get('X-API-Key')
        if not key or key != API_KEY:
            return jsonify({"error": "Unauthorized. Invalid or missing X-API-Key header."}), 401
        return f(*args, **kwargs)
    return decorated_function


@api_bp.route('/products', methods=['GET'])
@require_api_key
def api_get_products():
    sku_filter = request.args.get('sku', '').strip()
    query = Product.query
    if sku_filter:
        query = query.filter(Product.sku.ilike(f"%{sku_filter}%"))
    products = query.order_by(Product.name).all()

    res = []
    for p in products:
        res.append({
            "id": p.id,
            "name": p.name,
            "sku": p.sku,
            "quantity": p.quantity,
            "price": p.price,
            "category_name": p.category.name if p.category else None,
            "supplier_name": p.supplier.name if p.supplier else None,
            "tags": [t.name for t in p.tags]
        })
    return jsonify(res)


@api_bp.route('/products/<int:pid>', methods=['GET'])
@require_api_key
def api_get_product(pid):
    p = Product.query.get(pid)
    if not p:
        return jsonify({"error": "Product not found"}), 404
    return jsonify({
        "id": p.id,
        "name": p.name,
        "sku": p.sku,
        "quantity": p.quantity,
        "price": p.price,
        "category_name": p.category.name if p.category else None,
        "supplier_name": p.supplier.name if p.supplier else None,
        "tags": [t.name for t in p.tags]
    })


@api_bp.route('/products', methods=['POST'])
@require_api_key
def api_create_product():
    data = request.get_json(silent=True) or {}
    name = data.get('name', '').strip()
    sku = data.get('sku', '').strip()
    price = data.get('price')
    quantity = int(data.get('quantity') or 0)

    if not name or not sku or price is None:
        return jsonify({"error": "Fields 'name', 'sku', and 'price' are required."}), 400

    if Product.query.filter_by(sku=sku).first():
        return jsonify({"error": f"SKU '{sku}' already exists."}), 409

    p = Product(
        name=name,
        sku=sku,
        price=float(price),
        quantity=quantity,
        category_id=data.get('category_id'),
        supplier_id=data.get('supplier_id')
    )
    db.session.add(p)
    db.session.commit()

    # Assign initial quantity to default warehouse
    wh = get_default_warehouse()
    ws = WarehouseStock(warehouse_id=wh.id, product_id=p.id, quantity=quantity)
    db.session.add(ws)
    db.session.commit()

    log_activity(f"API: Created product {p.name}", "Product", p.id, p.name)
    return jsonify({"message": "Product created", "id": p.id}), 201


@api_bp.route('/products/<int:pid>', methods=['PUT'])
@require_api_key
def api_update_product(pid):
    p = Product.query.get(pid)
    if not p:
        return jsonify({"error": "Product not found"}), 404

    data = request.get_json(silent=True) or {}
    if 'name' in data:
        p.name = data['name'].strip()
    if 'price' in data:
        p.price = float(data['price'])
    if 'category_id' in data:
        p.category_id = data['category_id']
    if 'supplier_id' in data:
        p.supplier_id = data['supplier_id']

    db.session.commit()
    log_activity(f"API: Updated product {p.name}", "Product", p.id, p.name)
    return jsonify({"message": "Product updated", "id": p.id})


@api_bp.route('/products/<int:pid>', methods=['DELETE'])
@require_api_key
def api_delete_product(pid):
    p = Product.query.get(pid)
    if not p:
        return jsonify({"error": "Product not found"}), 404
    p_name = p.name
    db.session.delete(p)
    db.session.commit()
    log_activity(f"API: Deleted product {p_name}", "Product", pid, p_name)
    return jsonify({"message": f"Product '{p_name}' deleted."})


@api_bp.route('/customers', methods=['GET'])
@require_api_key
def api_get_customers():
    customers = Customer.query.order_by(Customer.name).all()
    res = []
    for c in customers:
        res.append({
            "id": c.id,
            "name": c.name,
            "email": c.email,
            "company": c.company,
            "status": c.status,
            "total_spent": c.total_spent
        })
    return jsonify(res)


@api_bp.route('/customers/<int:cid>', methods=['GET'])
@require_api_key
def api_get_customer(cid):
    c = Customer.query.get(cid)
    if not c:
        return jsonify({"error": "Customer not found"}), 404
    return jsonify({
        "id": c.id,
        "name": c.name,
        "email": c.email,
        "phone": c.phone,
        "company": c.company,
        "address": c.address,
        "status": c.status,
        "total_spent": c.total_spent,
        "invoices": [{"id": inv.id, "total": inv.total_amount, "date": inv.created_at.isoformat()} for inv in c.invoices]
    })


@api_bp.route('/customers', methods=['POST'])
@require_api_key
def api_create_customer():
    data = request.get_json(silent=True) or {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({"error": "Field 'name' is required."}), 400

    c = Customer(
        name=name,
        email=data.get('email', '').strip() or None,
        phone=data.get('phone', '').strip() or None,
        company=data.get('company', '').strip() or None,
        address=data.get('address', '').strip() or None,
        notes=data.get('notes', '').strip() or None,
        status=data.get('status', 'lead')
    )
    db.session.add(c)
    db.session.commit()
    log_activity(f"API: Created customer {c.name}", "Customer", c.id, c.name)
    return jsonify({"message": "Customer created", "id": c.id}), 201


@api_bp.route('/customers/<int:cid>', methods=['PUT'])
@require_api_key
def api_update_customer(cid):
    c = Customer.query.get(cid)
    if not c:
        return jsonify({"error": "Customer not found"}), 404
    data = request.get_json(silent=True) or {}
    for field in ['name', 'email', 'phone', 'company', 'address', 'notes', 'status']:
        if field in data:
            setattr(c, field, data[field].strip() if isinstance(data[field], str) else data[field])
    db.session.commit()
    log_activity(f"API: Updated customer {c.name}", "Customer", c.id, c.name)
    return jsonify({"message": "Customer updated", "id": c.id})


@api_bp.route('/invoices', methods=['GET'])
@require_api_key
def api_get_invoices():
    invoices = Invoice.query.order_by(Invoice.created_at.desc()).all()
    res = []
    for inv in invoices:
        res.append({
            "id": inv.id,
            "client_name": inv.client_name,
            "date": inv.created_at.isoformat(),
            "subtotal": inv.subtotal,
            "discount_pct": inv.discount_pct,
            "total": inv.total_amount,
            "currency": inv.currency_code
        })
    return jsonify(res)


@api_bp.route('/invoices/<int:inv_id>', methods=['GET'])
@require_api_key
def api_get_invoice(inv_id):
    inv = Invoice.query.get(inv_id)
    if not inv:
        return jsonify({"error": "Invoice not found"}), 404
    return jsonify({
        "id": inv.id,
        "client_name": inv.client_name,
        "date": inv.created_at.isoformat(),
        "subtotal": inv.subtotal,
        "discount_pct": inv.discount_pct,
        "total": inv.total_amount,
        "currency": inv.currency_code,
        "items": [{
            "product_id": item.product_id,
            "product_name": item.product.name if item.product else None,
            "quantity": item.quantity,
            "unit_price": item.unit_price,
            "total_price": item.total_price
        } for item in inv.items]
    })


@api_bp.route('/stock/low', methods=['GET'])
@require_api_key
def api_stock_low():
    products = Product.query.filter(Product.quantity <= 10).order_by(Product.quantity).all()
    return jsonify([{
        "id": p.id,
        "name": p.name,
        "sku": p.sku,
        "quantity": p.quantity,
        "status": "out_of_stock" if p.quantity == 0 else ("critical" if p.quantity <= 5 else "low")
    } for p in products])


@api_bp.route('/stock/adjust', methods=['POST'])
@require_api_key
def api_stock_adjust():
    data = request.get_json(silent=True) or {}
    product_id = data.get('product_id')
    adjustment = data.get('adjustment')

    if not product_id or adjustment is None:
        return jsonify({"error": "Fields 'product_id' and 'adjustment' are required."}), 400

    product = Product.query.get(int(product_id))
    if not product:
        return jsonify({"error": "Product not found"}), 404

    try:
        adj = int(adjustment)
    except ValueError:
        return jsonify({"error": "Adjustment must be an integer."}), 400

    # Adjust stock in default warehouse
    wh = get_default_warehouse()
    ws = WarehouseStock.query.filter_by(warehouse_id=wh.id, product_id=product.id).first()
    if not ws:
        ws = WarehouseStock(warehouse_id=wh.id, product_id=product.id, quantity=max(0, product.quantity))
        db.session.add(ws)

    ws.quantity = max(0, ws.quantity + adj)
    db.session.commit()
    sync_product_quantity(product.id)

    sign = '+' if adj > 0 else ''
    log_activity(f"API: Adjusted stock for {product.name} ({sign}{adj} -> {product.quantity})",
                 "Product", product.id, product.name)

    return jsonify({
        "product_id": product.id,
        "name": product.name,
        "adjustment": adj,
        "new_quantity": product.quantity
    })


app.register_blueprint(api_bp)


# ── Feature 2 — API Documentation & Feature 3 Scanner ──────────
@app.route('/api/docs')
@login_required
def api_docs():
    return render_template('api_docs.html', api_key=API_KEY)


@app.route('/scanner')
@login_required
def scanner():
    return render_template('scanner.html', api_key=API_KEY)


# ── Feature 4 — Permissions Matrix Settings ────────────────────
@app.route('/settings/permissions')
@admin_required
def settings_permissions():
    return render_template('settings/permissions.html')


# ── Feature 1 & 5 — Email & Scheduled Tasks ────────────────────
@app.route('/settings/email', methods=['GET', 'POST'])
@admin_required
def settings_email():
    mail_config = {
        "server": app.config.get('MAIL_SERVER'),
        "port": app.config.get('MAIL_PORT'),
        "use_tls": app.config.get('MAIL_USE_TLS'),
        "username_masked": app.config.get('MAIL_USERNAME')[:3] + '***' if app.config.get('MAIL_USERNAME') else '(none)',
        "password_masked": '********' if app.config.get('MAIL_PASSWORD') else '(none)',
        "default_sender": app.config.get('MAIL_DEFAULT_SENDER')
    }

    if request.method == 'POST':
        test_recipient = request.form.get('test_recipient', '').strip()
        if not test_recipient:
            flash("Please provide a recipient email address.", "danger")
            return redirect(url_for('settings_email'))

        body = f"""
        <h2>StockFlow SMTP Connectivity Verification</h2>
        <p>This is an automated test dispatch from your StockFlow inventory instance.</p>
        <ul>
            <li><strong>Timestamp:</strong> {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}</li>
            <li><strong>SMTP Server:</strong> {app.config.get('MAIL_SERVER')}:{app.config.get('MAIL_PORT')}</li>
            <li><strong>Default Sender:</strong> {app.config.get('MAIL_DEFAULT_SENDER')}</li>
        </ul>
        <p>If you received this message, your outbound SMTP configuration is operational.</p>
        """
        ok, msg = send_email_safe("StockFlow SMTP Test Message", test_recipient, body)
        if ok:
            flash(f"Test email sent successfully to {test_recipient}!", "success")
        else:
            flash(f"Failed to send test email: {msg}", "danger")
        return redirect(url_for('settings_email'))

    return render_template('settings/email.html', mail_config=mail_config)


@app.route('/invoice/<int:invoice_id>/send-email', methods=['POST'])
@login_required
def invoice_send_email(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    recipient = request.form.get('recipient_email', '').strip()
    if not recipient and invoice.customer:
        recipient = (invoice.customer.email or '').strip()

    if not recipient:
        flash("No recipient email provided for this invoice dispatch.", "warning")
        return redirect(url_for('invoice_view', invoice_id=invoice.id))

    html_content = render_template('email/invoice.html', invoice=invoice)
    subject = f"Invoice #INV-{invoice.id:04d} from StockFlow"
    ok, msg = send_email_safe(subject, recipient, html_content)
    if ok:
        log_activity(f"Dispatched invoice #INV-{invoice.id:04d} via email to {recipient}", "Invoice", invoice.id, invoice.client_name)
        flash(f"Invoice email successfully sent to {recipient}.", "success")
    else:
        flash(f"Email could not be delivered: {msg}", "danger")
    return redirect(url_for('invoice_view', invoice_id=invoice.id))


@app.route('/alerts/send-digest', methods=['POST'])
@login_required
def alerts_send_digest():
    admin_users = User.query.filter_by(role='admin').all()
    recipients = [u.email for u in admin_users if u.email]
    if not recipients:
        flash("No administrator email addresses configured to receive low-stock digests.", "warning")
        return redirect(url_for('alerts'))

    products = Product.query.filter(Product.quantity <= 10).order_by(Product.quantity).all()
    out_of_stock = [p for p in products if p.quantity == 0]
    critical     = [p for p in products if 1 <= p.quantity <= 5]
    low          = [p for p in products if 6 <= p.quantity <= 10]

    html_content = render_template('email/low_stock.html',
                                   total_alerts=len(products),
                                   out_of_stock=out_of_stock,
                                   critical=critical,
                                   low=low)

    ok, msg = send_email_safe("StockFlow Inventory Alert: Low-Stock Digest", recipients, html_content)
    if ok:
        log_activity("Triggered on-demand Low-Stock Alert email digest", "System", None, "Digest")
        flash(f"Low-stock digest sent to {len(recipients)} admin recipient(s).", "success")
    else:
        flash(f"Failed to dispatch low-stock digest: {msg}", "danger")
    return redirect(url_for('alerts'))


@app.route('/purchase-order/<int:poid>/send', methods=['POST'])
@require_permission('po_manage')
def purchase_order_send(poid):
    po = PurchaseOrder.query.get_or_404(poid)
    po.status = 'sent'
    db.session.commit()

    if po.supplier and po.supplier.email:
        html = render_template('email/po_sent.html', po=po)
        send_email_safe(f"Purchase Order #PO-{po.id:04d} from StockFlow", po.supplier.email, html)

    log_activity(f"Marked PO-{po.id:04d} as sent to {po.supplier.name}", "PurchaseOrder", po.id, f"PO-{po.id:04d}")
    flash(f"Purchase Order #PO-{po.id:04d} marked as sent. Vendor email dispatched if configured.")
    return redirect(url_for('purchase_order_detail', poid=po.id))


# ── Feature 5 — APScheduler Automated Jobs & Settings ──────────
JOBS_METADATA = {
    'daily_low_stock': {
        'name': 'Daily Low-Stock Alert Digest',
        'description': 'Scans inventory for items with quantity <= 10 and dispatches an HTML summary to admin team.',
        'cron_display': 'Every day at 08:00 AM',
        'next_run_hint': 'Tomorrow at 08:00 AM'
    },
    'weekly_sales_summary': {
        'name': 'Weekly Executive Sales Summary',
        'description': 'Aggregates weekly revenue, order totals, top-moving SKUs and new accounts.',
        'cron_display': 'Every Monday at 09:00 AM',
        'next_run_hint': 'Next Monday at 09:00 AM'
    }
}


def execute_job(job_id):
    cfg = ScheduleConfig.query.filter_by(job_id=job_id).first()
    if not cfg:
        cfg = ScheduleConfig(job_id=job_id, enabled=True)
        db.session.add(cfg)

    cfg.last_run = datetime.utcnow()

    try:
        if job_id == 'daily_low_stock':
            admins = User.query.filter_by(role='admin').all()
            recipients = [u.email for u in admins if u.email]
            if recipients:
                products = Product.query.filter(Product.quantity <= 10).order_by(Product.quantity).all()
                if products:
                    out_of_stock = [p for p in products if p.quantity == 0]
                    critical = [p for p in products if 1 <= p.quantity <= 5]
                    low = [p for p in products if 6 <= p.quantity <= 10]
                    html = render_template('email/low_stock.html',
                                           total_alerts=len(products),
                                           out_of_stock=out_of_stock,
                                           critical=critical,
                                           low=low)
                    send_email_safe("Daily Inventory Digest: Low Stock Notice", recipients, html)
            cfg.last_status = "Completed Successfully"

        elif job_id == 'weekly_sales_summary':
            admins = User.query.filter_by(role='admin').all()
            recipients = [u.email for u in admins if u.email]
            if recipients:
                one_week_ago = datetime.utcnow() - timedelta(days=7)
                invoices = Invoice.query.filter(Invoice.created_at >= one_week_ago).all()
                total_rev = sum(inv.total_amount for inv in invoices)
                new_cust = Customer.query.filter(Customer.created_at >= one_week_ago).count()

                # Top products
                items = InvoiceItem.query.join(Invoice).filter(Invoice.created_at >= one_week_ago).all()
                prod_counts = {}
                for item in items:
                    name = item.product.name if item.product else f"Product #{item.product_id}"
                    prod_counts[name] = prod_counts.get(name, 0) + item.quantity
                sorted_prods = sorted(prod_counts.items(), key=lambda x: x[1], reverse=True)[:5]

                html = render_template('email/weekly_summary.html',
                                       start_date=one_week_ago.strftime('%b %d'),
                                       end_date=datetime.utcnow().strftime('%b %d, %Y'),
                                       total_revenue=total_rev,
                                       invoices_count=len(invoices),
                                       new_customers_count=new_cust,
                                       top_products=sorted_prods)
                send_email_safe("Weekly Executive Sales Summary", recipients, html)
            cfg.last_status = "Completed Successfully"

        db.session.commit()
    except Exception as e:
        cfg.last_status = f"Failed: {str(e)[:50]}"
        db.session.commit()


@app.route('/settings/schedules')
@admin_required
def settings_schedules():
    jobs_list = []
    for jid, meta in JOBS_METADATA.items():
        cfg = ScheduleConfig.query.filter_by(job_id=jid).first()
        jobs_list.append({
            "job_id": jid,
            "name": meta['name'],
            "description": meta['description'],
            "cron_display": meta['cron_display'],
            "next_run": meta['next_run_hint'] if (cfg and cfg.enabled) else None,
            "last_run": cfg.last_run if cfg else None,
            "last_status": cfg.last_status if cfg else 'Never run',
            "enabled": cfg.enabled if cfg else True
        })
    return render_template('settings/schedules.html', jobs=jobs_list)


@app.route('/settings/schedule/<job_id>/toggle', methods=['POST'])
@admin_required
def settings_schedule_toggle(job_id):
    cfg = ScheduleConfig.query.filter_by(job_id=job_id).first()
    if not cfg:
        cfg = ScheduleConfig(job_id=job_id, enabled=True)
        db.session.add(cfg)
    cfg.enabled = not cfg.enabled
    db.session.commit()
    state = "enabled" if cfg.enabled else "disabled"
    flash(f"Job '{job_id}' is now {state}.")
    return redirect(url_for('settings_schedules'))


@app.route('/settings/schedule/<job_id>/run', methods=['POST'])
@admin_required
def settings_schedule_run(job_id):
    execute_job(job_id)
    flash(f"Triggered manual execution for '{job_id}'. Check status below.")
    return redirect(url_for('settings_schedules'))


# ── Feature 6 — Multi-Warehouse Management ─────────────────────
@app.route('/warehouses')
@login_required
def warehouse_list():
    warehouses = Warehouse.query.order_by(Warehouse.is_default.desc(), Warehouse.name).all()
    return render_template('warehouses.html', warehouses=warehouses)


@app.route('/warehouse/new', methods=['GET', 'POST'])
@require_permission('warehouse_manage')
def warehouse_create():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        location = request.form.get('location', '').strip() or None
        notes = request.form.get('notes', '').strip() or None
        is_default = bool(request.form.get('is_default'))

        if not name:
            flash("Warehouse name is required.", "danger")
            return redirect(url_for('warehouse_create'))

        if is_default:
            Warehouse.query.update({Warehouse.is_default: False})

        wh = Warehouse(name=name, location=location, notes=notes, is_default=is_default)
        db.session.add(wh)
        db.session.commit()

        # Initialize stock entries for all existing products
        all_prods = Product.query.all()
        for p in all_prods:
            db.session.add(WarehouseStock(warehouse_id=wh.id, product_id=p.id, quantity=0))
        db.session.commit()

        log_activity(f"Created warehouse {wh.name}", "Warehouse", wh.id, wh.name)
        flash(f"Warehouse '{wh.name}' created.")
        return redirect(url_for('warehouse_list'))
    return render_template('warehouse_form.html', warehouse=None)


@app.route('/warehouse/<int:wid>')
@login_required
def warehouse_detail(wid):
    wh = Warehouse.query.get_or_404(wid)
    stock_entries = WarehouseStock.query.filter_by(warehouse_id=wh.id).join(Product).order_by(Product.name).all()
    recent_transfers_count = StockTransfer.query.filter(
        (StockTransfer.from_warehouse_id == wid) | (StockTransfer.to_warehouse_id == wid)
    ).count()
    return render_template('warehouse_detail.html',
                           warehouse=wh,
                           stock_entries=stock_entries,
                           recent_transfers_count=recent_transfers_count)


@app.route('/warehouse/<int:wid>/edit', methods=['GET', 'POST'])
@require_permission('warehouse_manage')
def warehouse_edit(wid):
    wh = Warehouse.query.get_or_404(wid)
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        location = request.form.get('location', '').strip() or None
        notes = request.form.get('notes', '').strip() or None
        is_default = bool(request.form.get('is_default'))

        if not name:
            flash("Warehouse name is required.", "danger")
            return redirect(url_for('warehouse_edit', wid=wh.id))

        if is_default and not wh.is_default:
            Warehouse.query.update({Warehouse.is_default: False})

        wh.name = name
        wh.location = location
        wh.notes = notes
        wh.is_default = is_default or wh.is_default

        db.session.commit()
        log_activity(f"Updated warehouse {wh.name}", "Warehouse", wh.id, wh.name)
        flash(f"Warehouse '{wh.name}' updated.")
        return redirect(url_for('warehouse_detail', wid=wh.id))
    return render_template('warehouse_form.html', warehouse=wh)


@app.route('/warehouse/<int:wid>/delete', methods=['POST'])
@require_permission('warehouse_manage')
def warehouse_delete(wid):
    wh = Warehouse.query.get_or_404(wid)
    if wh.is_default:
        flash("Cannot delete the default warehouse facility.", "danger")
        return redirect(url_for('warehouse_list'))

    if wh.total_units > 0:
        flash("Cannot delete a warehouse that currently holds stock units. Transfer inventory first.", "danger")
        return redirect(url_for('warehouse_list'))

    wh_name = wh.name
    db.session.delete(wh)
    db.session.commit()
    log_activity(f"Deleted warehouse {wh_name}", "Warehouse", wid, wh_name)
    flash(f"Warehouse '{wh_name}' deleted.")
    return redirect(url_for('warehouse_list'))


@app.route('/warehouse/<int:wid>/stock', methods=['GET', 'POST'])
@require_permission('warehouse_manage')
def warehouse_stock_manage(wid):
    wh = Warehouse.query.get_or_404(wid)
    if request.method == 'POST':
        product_ids = request.form.getlist('product_ids')
        quantities  = request.form.getlist('quantities')

        for pid_str, qty_str in zip(product_ids, quantities):
            try:
                pid = int(pid_str)
                qty = max(0, int(qty_str))
            except ValueError:
                continue

            ws = WarehouseStock.query.filter_by(warehouse_id=wh.id, product_id=pid).first()
            if not ws:
                ws = WarehouseStock(warehouse_id=wh.id, product_id=pid, quantity=qty)
                db.session.add(ws)
            else:
                ws.quantity = qty

        db.session.commit()

        # Resynchronize total product quantities across warehouses
        for pid_str in product_ids:
            try:
                sync_product_quantity(int(pid_str))
            except ValueError:
                pass

        log_activity(f"Updated stock levels for warehouse {wh.name}", "Warehouse", wh.id, wh.name)
        flash(f"Stock levels updated for {wh.name}.")
        return redirect(url_for('warehouse_detail', wid=wh.id))

    all_products = Product.query.order_by(Product.name).all()
    entries_map = {ws.product_id: ws.quantity for ws in wh.stock_entries}
    product_stock_items = []
    for p in all_products:
        product_stock_items.append({
            "product": p,
            "quantity": entries_map.get(p.id, 0)
        })

    return render_template('warehouse_stock.html', warehouse=wh, product_stock_items=product_stock_items)


@app.route('/warehouse/transfer', methods=['GET', 'POST'])
@require_permission('warehouse_manage')
def warehouse_transfer_create():
    warehouses = Warehouse.query.order_by(Warehouse.name).all()
    products = Product.query.order_by(Product.name).all()

    if request.method == 'POST':
        from_id = int(request.form['from_warehouse_id'])
        to_id   = int(request.form['to_warehouse_id'])
        pid     = int(request.form['product_id'])
        qty     = int(request.form.get('quantity') or 0)

        if from_id == to_id:
            flash("Source and destination warehouses must be different.", "danger")
            return redirect(url_for('warehouse_transfer_create'))

        if qty <= 0:
            flash("Transfer quantity must be greater than zero.", "danger")
            return redirect(url_for('warehouse_transfer_create'))

        source_stock = WarehouseStock.query.filter_by(warehouse_id=from_id, product_id=pid).first()
        if not source_stock or source_stock.quantity < qty:
            flash(f"Insufficient stock in source facility. Available: {source_stock.quantity if source_stock else 0}", "danger")
            return redirect(url_for('warehouse_transfer_create'))

        dest_stock = WarehouseStock.query.filter_by(warehouse_id=to_id, product_id=pid).first()
        if not dest_stock:
            dest_stock = WarehouseStock(warehouse_id=to_id, product_id=pid, quantity=0)
            db.session.add(dest_stock)

        source_stock.quantity -= qty
        dest_stock.quantity += qty

        transfer = StockTransfer(
            from_warehouse_id=from_id,
            to_warehouse_id=to_id,
            product_id=pid,
            quantity=qty,
            user_id=current_user.id
        )
        db.session.add(transfer)
        db.session.commit()

        sync_product_quantity(pid)

        from_wh = Warehouse.query.get(from_id)
        to_wh   = Warehouse.query.get(to_id)
        prod    = Product.query.get(pid)

        log_activity(f"Transferred {qty} units of {prod.name} from {from_wh.name} to {to_wh.name}",
                     "WarehouseTransfer", transfer.id, f"{qty}x {prod.name}")

        flash(f"Transferred {qty} units of {prod.name} from {from_wh.name} to {to_wh.name} successfully.")
        return redirect(url_for('warehouse_transfers'))

    # Build stock map JSON for live client-side validation
    stock_entries = WarehouseStock.query.all()
    stock_map = {}
    for se in stock_entries:
        if se.warehouse_id not in stock_map:
            stock_map[se.warehouse_id] = {}
        stock_map[se.warehouse_id][se.product_id] = se.quantity

    return render_template('warehouse_transfer.html',
                           warehouses=warehouses,
                           products=products,
                           stock_map_json=json.dumps(stock_map))


@app.route('/warehouse/transfers')
@login_required
def warehouse_transfers():
    transfers = StockTransfer.query.order_by(StockTransfer.created_at.desc()).all()
    return render_template('warehouse_transfers.html', transfers=transfers)


# ── Routes — Authentication & Users ───────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        login_input = request.form.get('username', '').strip()
        password    = request.form.get('password', '')
        user = User.query.filter((User.username == login_input) | (User.email == login_input)).first()
        if user and user.check_password(password):
            if not user.is_active:
                flash('This account is deactivated. Please contact an administrator.', 'danger')
                return render_template('login.html')
            login_user(user)
            log_activity("User logged in", "User", user.id, user.username)
            flash(f"Welcome back, {user.username}!")
            next_page = request.args.get('next')
            return redirect(next_page or url_for('index'))
        flash('Invalid username or password.', 'danger')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    log_activity("User logged out", "User", current_user.id, current_user.username)
    logout_user()
    flash('You have been logged out.')
    return redirect(url_for('login'))


@app.route('/register', methods=['GET', 'POST'])
def register():
    has_users = User.query.first() is not None
    if has_users:
        if not current_user.is_authenticated or current_user.role != 'admin':
            abort(403)

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email    = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        role     = request.form.get('role', 'staff') if (current_user.is_authenticated and current_user.role == 'admin') else ('admin' if not has_users else 'staff')

        if not username or not email or not password:
            flash('All fields are required.', 'danger')
            return render_template('user_form.html', is_register=True, edit_user=None)

        if User.query.filter_by(username=username).first():
            flash('Username is already taken.', 'danger')
            return render_template('user_form.html', is_register=True, edit_user=None)

        if User.query.filter_by(email=email).first():
            flash('Email is already registered.', 'danger')
            return render_template('user_form.html', is_register=True, edit_user=None)

        user = User(username=username, email=email, role=role, is_active=True)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        log_activity(f"Created user {user.username} ({user.role})", "User", user.id, user.username)

        if not has_users:
            login_user(user)
            flash('Admin account created! Welcome to StockFlow.')
            return redirect(url_for('index'))

        flash(f"User {user.username} created successfully.")
        return redirect(url_for('user_list'))

    return render_template('user_form.html', is_register=True, edit_user=None)


@app.route('/users')
@admin_required
def user_list():
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template('users.html', users=users)


@app.route('/user/<int:uid>/edit', methods=['GET', 'POST'])
@admin_required
def user_edit(uid):
    user = User.query.get_or_404(uid)
    if request.method == 'POST':
        role      = request.form.get('role', user.role)
        is_active = bool(request.form.get('is_active'))
        new_pass  = request.form.get('new_password', '').strip()

        if user.id == current_user.id:
            role = 'admin'
            is_active = True

        user.role = role
        user.is_active = is_active
        if new_pass:
            user.set_password(new_pass)
            flash(f"Password reset for {user.username}.")

        db.session.commit()
        log_activity(f"Updated user {user.username}", "User", user.id, user.username)
        flash(f"User {user.username} updated.")
        return redirect(url_for('user_list'))
    return render_template('user_form.html', is_register=False, edit_user=user)


# ── Routes — Dashboard & Inventory ────────────────────────────
@app.route('/')
@login_required
def index():
    tag_id = request.args.get('tag', type=int)
    active_tag = Tag.query.get(tag_id) if tag_id else None

    if active_tag:
        products = Product.query.filter(Product.tags.any(id=active_tag.id)).all()
    else:
        products = Product.query.all()

    customers = Customer.query.all()
    invoices  = Invoice.query.order_by(Invoice.created_at.desc()).limit(5).all()
    return render_template('index.html',
                           products=products,
                           customers=customers,
                           invoices=invoices,
                           active_tag=active_tag)


@app.route('/products')
@login_required
def product_list():
    return redirect(url_for('index', **request.args))


# ── Routes — Product CRUD ─────────────────────────────────────
@app.route('/product/new', methods=['GET', 'POST'])
@require_permission('create')
def product_create():
    categories = Category.query.order_by(Category.name).all()
    suppliers  = Supplier.query.order_by(Supplier.name).all()
    all_tags   = Tag.query.order_by(Tag.name).all()

    if request.method == 'POST':
        image_file  = request.files.get('image')
        cat_id      = request.form.get('category_id')
        sup_id      = request.form.get('supplier_id')
        tag_ids     = request.form.getlist('tag_ids')
        quantity    = int(request.form.get('quantity') or 0)

        product = Product(
            name        = request.form['name'].strip(),
            sku         = request.form['sku'].strip(),
            quantity    = quantity,
            price       = float(request.form.get('price') or 0),
            category_id = int(cat_id) if cat_id else None,
            supplier_id = int(sup_id) if sup_id else None,
            image       = save_upload(image_file),
        )

        if tag_ids:
            product.tags = Tag.query.filter(Tag.id.in_([int(tid) for tid in tag_ids])).all()

        db.session.add(product)
        db.session.commit()

        # Seed warehouse stock entry into default facility
        wh = get_default_warehouse()
        ws = WarehouseStock(warehouse_id=wh.id, product_id=product.id, quantity=quantity)
        db.session.add(ws)
        db.session.commit()

        log_activity(f"Created product {product.name}", "Product", product.id, product.name)
        flash('Product added successfully')
        return redirect(url_for('index'))

    return render_template('product_form.html', product=None,
                           categories=categories, suppliers=suppliers, all_tags=all_tags)


@app.route('/product/<int:pid>/edit', methods=['GET', 'POST'])
@require_permission('edit')
def product_edit(pid):
    product    = Product.query.get_or_404(pid)
    categories = Category.query.order_by(Category.name).all()
    suppliers  = Supplier.query.order_by(Supplier.name).all()
    all_tags   = Tag.query.order_by(Tag.name).all()

    if request.method == 'POST':
        product.name     = request.form['name'].strip()
        product.sku      = request.form['sku'].strip()
        product.price    = float(request.form.get('price') or 0)
        cat_id           = request.form.get('category_id')
        sup_id           = request.form.get('supplier_id')
        tag_ids          = request.form.getlist('tag_ids')

        product.category_id = int(cat_id) if cat_id else None
        product.supplier_id = int(sup_id) if sup_id else None
        product.tags = Tag.query.filter(Tag.id.in_([int(tid) for tid in tag_ids])).all() if tag_ids else []

        image_file = request.files.get('image')
        if image_file and image_file.filename:
            if product.image:
                old_path = os.path.join(app.config['UPLOAD_FOLDER'], product.image)
                if os.path.exists(old_path):
                    os.remove(old_path)
            product.image = save_upload(image_file)

        db.session.commit()
        log_activity(f"Updated product {product.name}", "Product", product.id, product.name)
        flash('Product updated')
        return redirect(url_for('index'))

    return render_template('product_form.html', product=product,
                           categories=categories, suppliers=suppliers, all_tags=all_tags)


@app.route('/product/<int:pid>/delete', methods=['POST'])
@require_permission('delete_product')
def product_delete(pid):
    product = Product.query.get_or_404(pid)
    p_name = product.name
    if product.image:
        old_path = os.path.join(app.config['UPLOAD_FOLDER'], product.image)
        if os.path.exists(old_path):
            os.remove(old_path)
    db.session.delete(product)
    db.session.commit()
    log_activity(f"Deleted product {p_name}", "Product", pid, p_name)
    flash('Product deleted')
    return redirect(url_for('index'))


@app.route('/product/<int:pid>/adjust-stock', methods=['POST'])
@login_required
def product_adjust_stock(pid):
    product = Product.query.get_or_404(pid)
    try:
        adjustment = int(request.form.get('adjustment') or 0)
    except ValueError:
        adjustment = 0

    wh = get_default_warehouse()
    ws = WarehouseStock.query.filter_by(warehouse_id=wh.id, product_id=product.id).first()
    if not ws:
        ws = WarehouseStock(warehouse_id=wh.id, product_id=product.id, quantity=max(0, product.quantity))
        db.session.add(ws)

    ws.quantity = max(0, ws.quantity + adjustment)
    db.session.commit()
    sync_product_quantity(product.id)

    sign = '+' if adjustment > 0 else ''
    log_activity(f"Adjusted stock for {product.name} ({sign}{adjustment} -> {product.quantity})",
                 "Product", product.id, product.name)
    flash(f"Stock adjusted for {product.name} ({sign}{adjustment} → new quantity: {product.quantity})")
    return redirect(request.referrer or url_for('alerts'))


# ── QR Code & Barcode Routes ──────────────────────────────────
@app.route('/product/<int:pid>/qrcode')
@login_required
def product_qrcode(pid):
    product = Product.query.get_or_404(pid)
    data = json.dumps({
        "id": product.id,
        "sku": product.sku,
        "name": product.name,
        "price": product.price
    })
    qr_img = qrcode.make(data)
    buf = BytesIO()
    qr_img.save(buf, format='PNG')
    buf.seek(0)
    return send_file(buf, mimetype='image/png')


@app.route('/product/<int:pid>/barcode')
@login_required
def product_barcode(pid):
    product = Product.query.get_or_404(pid)
    code128 = barcode.get('code128', product.sku, writer=ImageWriter())
    buf = BytesIO()
    code128.write(buf)
    buf.seek(0)
    return send_file(buf, mimetype='image/png')


# ── Routes — Categories ───────────────────────────────────────
@app.route('/categories')
@login_required
def category_list():
    categories = Category.query.order_by(Category.name).all()
    return render_template('categories.html', categories=categories)


@app.route('/category/new', methods=['GET', 'POST'])
@require_permission('create')
def category_create():
    if request.method == 'POST':
        name  = request.form.get('name', '').strip()
        color = request.form.get('color', '#4f46e5').strip() or '#4f46e5'
        icon  = request.form.get('icon', 'bi-tag').strip() or 'bi-tag'
        if not name:
            flash('Category name is required')
            return redirect(url_for('category_create'))
        category = Category(name=name, color=color, icon=icon)
        db.session.add(category)
        db.session.commit()
        log_activity(f"Created category {category.name}", "Category", category.id, category.name)
        flash('Category created successfully')
        return redirect(url_for('category_list'))
    return render_template('category_form.html', category=None)


@app.route('/category/<int:cid>/edit', methods=['GET', 'POST'])
@require_permission('edit')
def category_edit(cid):
    category = Category.query.get_or_404(cid)
    if request.method == 'POST':
        category.name  = request.form.get('name', '').strip() or category.name
        category.color = request.form.get('color', '#4f46e5').strip() or category.color
        category.icon  = request.form.get('icon', 'bi-tag').strip() or category.icon
        db.session.commit()
        log_activity(f"Updated category {category.name}", "Category", category.id, category.name)
        flash('Category updated')
        return redirect(url_for('category_list'))
    return render_template('category_form.html', category=category)


@app.route('/category/<int:cid>/delete', methods=['POST'])
@admin_required
def category_delete(cid):
    category = Category.query.get_or_404(cid)
    c_name = category.name
    for p in category.products:
        p.category_id = None
    db.session.delete(category)
    db.session.commit()
    log_activity(f"Deleted category {c_name}", "Category", cid, c_name)
    flash('Category deleted')
    return redirect(url_for('category_list'))


# ── Routes — Tags ─────────────────────────────────────────────
@app.route('/tags')
@login_required
def tag_list():
    tags = Tag.query.order_by(Tag.name).all()
    return render_template('tags.html', tags=tags)


@app.route('/tag/new', methods=['GET', 'POST'])
@require_permission('create')
def tag_create():
    if request.method == 'POST':
        name  = request.form.get('name', '').strip()
        color = request.form.get('color', '#6366f1').strip() or '#6366f1'
        if not name:
            flash('Tag name is required.', 'danger')
            return render_template('tag_form.html', tag=None)
        if Tag.query.filter_by(name=name).first():
            flash('A tag with that name already exists.', 'danger')
            return render_template('tag_form.html', tag=None)
        tag = Tag(name=name, color=color)
        db.session.add(tag)
        db.session.commit()
        log_activity(f"Created tag #{tag.name}", "Tag", tag.id, tag.name)
        flash('Tag created successfully.')
        return redirect(url_for('tag_list'))
    return render_template('tag_form.html', tag=None)


@app.route('/tag/<int:tid>/edit', methods=['GET', 'POST'])
@require_permission('edit')
def tag_edit(tid):
    tag = Tag.query.get_or_404(tid)
    if request.method == 'POST':
        name  = request.form.get('name', '').strip()
        color = request.form.get('color', '#6366f1').strip() or '#6366f1'
        if not name:
            flash('Tag name is required.', 'danger')
            return render_template('tag_form.html', tag=tag)
        existing = Tag.query.filter_by(name=name).first()
        if existing and existing.id != tag.id:
            flash('A tag with that name already exists.', 'danger')
            return render_template('tag_form.html', tag=tag)
        tag.name  = name
        tag.color = color
        db.session.commit()
        log_activity(f"Updated tag #{tag.name}", "Tag", tag.id, tag.name)
        flash('Tag updated.')
        return redirect(url_for('tag_list'))
    return render_template('tag_form.html', tag=tag)


@app.route('/tag/<int:tid>/delete', methods=['POST'])
@admin_required
def tag_delete(tid):
    tag = Tag.query.get_or_404(tid)
    t_name = tag.name
    db.session.delete(tag)
    db.session.commit()
    log_activity(f"Deleted tag #{t_name}", "Tag", tid, t_name)
    flash('Tag deleted.')
    return redirect(url_for('tag_list'))


# ── Routes — Suppliers ────────────────────────────────────────
@app.route('/suppliers')
@login_required
def supplier_list():
    suppliers = Supplier.query.order_by(Supplier.name).all()
    return render_template('suppliers.html', suppliers=suppliers)


@app.route('/supplier/new', methods=['GET', 'POST'])
@require_permission('create')
def supplier_create():
    if request.method == 'POST':
        supplier = Supplier(
            name    = request.form['name'].strip(),
            email   = request.form.get('email', '').strip() or None,
            phone   = request.form.get('phone', '').strip() or None,
            address = request.form.get('address', '').strip() or None,
            notes   = request.form.get('notes', '').strip() or None,
        )
        db.session.add(supplier)
        db.session.commit()
        log_activity(f"Created supplier {supplier.name}", "Supplier", supplier.id, supplier.name)
        flash('Supplier added successfully')
        return redirect(url_for('supplier_detail', sid=supplier.id))
    return render_template('supplier_form.html', supplier=None)


@app.route('/supplier/<int:sid>')
@login_required
def supplier_detail(sid):
    supplier = Supplier.query.get_or_404(sid)
    return render_template('supplier_detail.html', supplier=supplier)


@app.route('/supplier/<int:sid>/edit', methods=['GET', 'POST'])
@require_permission('edit')
def supplier_edit(sid):
    supplier = Supplier.query.get_or_404(sid)
    if request.method == 'POST':
        supplier.name    = request.form['name'].strip()
        supplier.email   = request.form.get('email', '').strip() or None
        supplier.phone   = request.form.get('phone', '').strip() or None
        supplier.address = request.form.get('address', '').strip() or None
        supplier.notes   = request.form.get('notes', '').strip() or None
        db.session.commit()
        log_activity(f"Updated supplier {supplier.name}", "Supplier", supplier.id, supplier.name)
        flash('Supplier updated')
        return redirect(url_for('supplier_detail', sid=supplier.id))
    return render_template('supplier_form.html', supplier=supplier)


@app.route('/supplier/<int:sid>/delete', methods=['POST'])
@require_permission('delete_supplier')
def supplier_delete(sid):
    supplier = Supplier.query.get_or_404(sid)
    s_name = supplier.name
    for p in supplier.products:
        p.supplier_id = None
    db.session.delete(supplier)
    db.session.commit()
    log_activity(f"Deleted supplier {s_name}", "Supplier", sid, s_name)
    flash('Supplier deleted')
    return redirect(url_for('supplier_list'))


# ── Routes — Purchase Orders ──────────────────────────────────
@app.route('/purchase-orders')
@login_required
def purchase_order_list():
    pos = PurchaseOrder.query.order_by(PurchaseOrder.created_at.desc()).all()
    return render_template('purchase_orders.html', purchase_orders=pos)


@app.route('/purchase-order/new', methods=['GET', 'POST'])
@require_permission('create')
def purchase_order_create():
    suppliers = Supplier.query.order_by(Supplier.name).all()
    products  = Product.query.order_by(Product.name).all()

    if request.method == 'POST':
        supplier_id = int(request.form.get('supplier_id'))
        notes       = request.form.get('notes', '').strip() or None
        po = PurchaseOrder(supplier_id=supplier_id, notes=notes, status='draft')
        db.session.add(po)
        db.session.flush()

        product_ids = request.form.getlist('product_id')
        quantities  = request.form.getlist('quantity')
        unit_costs  = request.form.getlist('unit_cost')

        for pid, qty, cost in zip(product_ids, quantities, unit_costs):
            if not pid or not qty or int(qty) <= 0:
                continue
            item = POItem(
                po_id=po.id,
                product_id=int(pid),
                quantity=int(qty),
                unit_cost=float(cost or 0.0)
            )
            db.session.add(item)

        db.session.commit()
        log_activity(f"Created Purchase Order PO-{po.id:04d}", "PurchaseOrder", po.id, f"PO-{po.id:04d}")
        flash(f"Purchase Order #PO-{po.id:04d} created successfully.")
        return redirect(url_for('purchase_order_detail', poid=po.id))

    selected_supplier_id = request.args.get('supplier_id', type=int)
    return render_template('purchase_order_form.html',
                           suppliers=suppliers, products=products,
                           selected_supplier_id=selected_supplier_id)


@app.route('/purchase-order/<int:poid>')
@login_required
def purchase_order_detail(poid):
    po = PurchaseOrder.query.get_or_404(poid)
    return render_template('purchase_order_detail.html', po=po)


@app.route('/purchase-order/<int:poid>/receive', methods=['POST'])
@require_permission('po_manage')
def purchase_order_receive(poid):
    po = PurchaseOrder.query.get_or_404(poid)
    if po.status == 'received':
        flash('Purchase Order is already marked as received.', 'info')
        return redirect(url_for('purchase_order_detail', poid=po.id))

    po.status = 'received'
    wh = get_default_warehouse()

    # Increment stock quantities in default warehouse & sync product total
    for item in po.items:
        if item.product:
            ws = WarehouseStock.query.filter_by(warehouse_id=wh.id, product_id=item.product.id).first()
            if not ws:
                ws = WarehouseStock(warehouse_id=wh.id, product_id=item.product.id, quantity=item.quantity)
                db.session.add(ws)
            else:
                ws.quantity += item.quantity
            sync_product_quantity(item.product.id)

    db.session.commit()
    log_activity(f"Received PO-{po.id:04d} and restocked into {wh.name}", "PurchaseOrder", po.id, f"PO-{po.id:04d}")
    flash(f"Purchase Order #PO-{po.id:04d} received. Products restocked into {wh.name}.")
    return redirect(url_for('purchase_order_detail', poid=po.id))


@app.route('/purchase-order/<int:poid>/delete', methods=['POST'])
@require_permission('po_manage')
def purchase_order_delete(poid):
    po = PurchaseOrder.query.get_or_404(poid)
    p_id = po.id
    db.session.delete(po)
    db.session.commit()
    log_activity(f"Deleted Purchase Order PO-{p_id:04d}", "PurchaseOrder", p_id, f"PO-{p_id:04d}")
    flash(f"Purchase Order #PO-{p_id:04d} deleted.")
    return redirect(url_for('purchase_order_list'))


# ── Routes — Invoices ─────────────────────────────────────────
@app.route('/invoices')
@login_required
def invoice_list():
    invoices = Invoice.query.order_by(Invoice.created_at.desc()).all()
    return render_template('invoice_list.html', invoices=invoices)


@app.route('/invoice/new', methods=['GET', 'POST'])
@require_permission('create')
def invoice_create():
    products   = Product.query.order_by(Product.name).all()
    customers  = Customer.query.order_by(Customer.name).all()
    currencies = Currency.query.order_by(Currency.code).all()
    warehouses = Warehouse.query.order_by(Warehouse.is_default.desc(), Warehouse.name).all()

    if request.method == 'POST':
        customer_id  = request.form.get('customer_id')
        currency_id  = request.form.get('currency_id')
        warehouse_id = request.form.get('warehouse_id')
        discount_pct = float(request.form.get('discount_pct') or 0.0)

        customer = Customer.query.get(int(customer_id)) if customer_id else None
        client_name = customer.name if customer else (request.form.get('client_name', '').strip() or 'Walk-in Client')

        # Determine fulfillment warehouse
        wh = Warehouse.query.get(int(warehouse_id)) if warehouse_id else get_default_warehouse()

        invoice = Invoice(
            client_name  = client_name,
            customer_id  = customer.id if customer else None,
            currency_id  = int(currency_id) if currency_id else None,
            discount_pct = max(0.0, min(100.0, discount_pct))
        )
        db.session.add(invoice)
        db.session.flush()

        product_ids = request.form.getlist('product_id')
        quantities  = request.form.getlist('quantity')

        for pid, qty in zip(product_ids, quantities):
            if not qty or int(qty) <= 0:
                continue
            product = Product.query.get(int(pid))
            if product is None:
                continue

            q = int(qty)
            item = InvoiceItem(
                invoice_id = invoice.id,
                product_id = product.id,
                quantity   = q,
                unit_price = product.price,
            )
            db.session.add(item)

            # Deduct quantity from chosen warehouse
            ws = WarehouseStock.query.filter_by(warehouse_id=wh.id, product_id=product.id).first()
            if ws:
                ws.quantity = max(0, ws.quantity - q)
            sync_product_quantity(product.id)

        db.session.commit()
        log_activity(f"Created Invoice INV-{invoice.id:04d} for {invoice.client_name}", "Invoice", invoice.id, invoice.client_name)
        flash('Invoice created successfully')
        return redirect(url_for('invoice_view', invoice_id=invoice.id))

    return render_template('invoice_form.html',
                           products=products, customers=customers,
                           currencies=currencies, warehouses=warehouses)


@app.route('/invoice/<int:invoice_id>')
@login_required
def invoice_view(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    return render_template('invoice.html', invoice=invoice)


@app.route('/invoice/<int:invoice_id>/pdf')
@login_required
def invoice_pdf(invoice_id):
    try:
        from weasyprint import HTML as WPHTML
    except ImportError:
        flash('WeasyPrint is not installed. Use the browser Print button instead.')
        return redirect(url_for('invoice_view', invoice_id=invoice_id))

    invoice = Invoice.query.get_or_404(invoice_id)
    html = render_template('invoice.html', invoice=invoice)
    pdf_bytes = WPHTML(string=html, base_url=request.host_url).write_pdf()
    return send_file(
        BytesIO(pdf_bytes),
        download_name=f"invoice_{invoice.id}.pdf",
        as_attachment=True,
        mimetype='application/pdf',
    )


# ── Routes — Returns & Refunds ────────────────────────────────
@app.route('/returns')
@login_required
def return_list():
    returns = Return.query.order_by(Return.created_at.desc()).all()
    return render_template('returns.html', returns=returns)


@app.route('/invoice/<int:invoice_id>/return', methods=['GET', 'POST'])
@login_required
def invoice_return_create(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)

    if invoice.returns:
        flash('A return request already exists for this invoice.', 'info')
        return redirect(url_for('invoice_view', invoice_id=invoice.id))

    if request.method == 'POST':
        reason = request.form.get('reason', '').strip()
        if not reason:
            flash('Please provide a reason for the return.', 'danger')
            return render_template('return_form.html', invoice=invoice)

        item_quantities = {}
        for item in invoice.items:
            qty_returned = int(request.form.get(f'return_qty_{item.id}') or 0)
            if qty_returned > 0:
                item_quantities[item] = min(qty_returned, item.quantity)

        if not item_quantities:
            flash('Please select at least one item and quantity to return.', 'danger')
            return render_template('return_form.html', invoice=invoice)

        ret = Return(invoice_id=invoice.id, reason=reason, status='pending')
        db.session.add(ret)
        db.session.flush()

        subtotal_refund = 0.0
        for item, qty in item_quantities.items():
            r_item = ReturnItem(
                return_id=ret.id,
                product_id=item.product_id,
                quantity=qty,
                unit_price=item.unit_price
            )
            db.session.add(r_item)
            subtotal_refund += qty * item.unit_price

        # Apply same discount percentage as original invoice
        discount_mult = 1.0 - ((invoice.discount_pct or 0.0) / 100.0)
        ret.refund_amount = round(subtotal_refund * discount_mult, 2)

        db.session.commit()
        log_activity(f"Submitted return request for INV-{invoice.id:04d}", "Return", ret.id, f"Return #{ret.id}")
        flash(f"Return request #{ret.id} submitted for review.")
        return redirect(url_for('return_list'))

    return render_template('return_form.html', invoice=invoice)


@app.route('/return/<int:rid>/approve', methods=['POST'])
@require_permission('returns_manage')
def return_approve(rid):
    ret = Return.query.get_or_404(rid)
    if ret.status == 'approved':
        flash('Return is already approved.', 'info')
        return redirect(url_for('return_list'))

    ret.status = 'approved'
    wh = get_default_warehouse()

    # Restock returned items into default warehouse & sync product total
    for item in ret.items:
        if item.product:
            ws = WarehouseStock.query.filter_by(warehouse_id=wh.id, product_id=item.product.id).first()
            if not ws:
                ws = WarehouseStock(warehouse_id=wh.id, product_id=item.product.id, quantity=item.quantity)
                db.session.add(ws)
            else:
                ws.quantity += item.quantity
            sync_product_quantity(item.product.id)

    db.session.commit()
    log_activity(f"Approved return #{ret.id} for INV-{ret.invoice_id:04d} (Items restocked into {wh.name})",
                 "Return", ret.id, f"Return #{ret.id}")
    flash(f"Return #{ret.id} approved. Items have been returned to inventory ({wh.name}).")
    return redirect(url_for('return_list'))


@app.route('/return/<int:rid>/reject', methods=['POST'])
@require_permission('returns_manage')
def return_reject(rid):
    ret = Return.query.get_or_404(rid)
    ret.status = 'rejected'
    db.session.commit()
    log_activity(f"Rejected return #{ret.id} for INV-{ret.invoice_id:04d}", "Return", ret.id, f"Return #{ret.id}")
    flash(f"Return #{ret.id} marked as rejected.")
    return redirect(url_for('return_list'))


# ── Routes — CRM ──────────────────────────────────────────────
@app.route('/customers')
@login_required
def customer_list():
    status_filter = request.args.get('status', 'all')
    query = Customer.query
    if status_filter != 'all':
        query = query.filter_by(status=status_filter)
    customers = query.order_by(Customer.created_at.desc()).all()
    return render_template('crm/customer_list.html',
                           customers=customers, status_filter=status_filter)


@app.route('/customer/new', methods=['GET', 'POST'])
@require_permission('create')
def customer_create():
    if request.method == 'POST':
        email = request.form.get('email', '').strip() or None
        customer = Customer(
            name    = request.form['name'].strip(),
            email   = email,
            phone   = request.form.get('phone', '').strip() or None,
            company = request.form.get('company', '').strip() or None,
            address = request.form.get('address', '').strip() or None,
            notes   = request.form.get('notes', '').strip() or None,
            status  = request.form.get('status', 'lead'),
        )
        db.session.add(customer)
        db.session.commit()
        log_activity(f"Created customer {customer.name}", "Customer", customer.id, customer.name)

        # Trigger automated welcome email if email provided
        if customer.email:
            welcome_html = render_template('email/welcome.html', customer=customer)
            send_email_safe("Welcome to StockFlow!", customer.email, welcome_html)

        flash('Customer added successfully')
        return redirect(url_for('customer_detail', cid=customer.id))
    return render_template('crm/customer_form.html', customer=None)


@app.route('/customer/<int:cid>')
@login_required
def customer_detail(cid):
    customer = Customer.query.get_or_404(cid)
    return render_template('crm/customer_detail.html', customer=customer)


@app.route('/customer/<int:cid>/edit', methods=['GET', 'POST'])
@require_permission('edit')
def customer_edit(cid):
    customer = Customer.query.get_or_404(cid)
    if request.method == 'POST':
        customer.name    = request.form['name'].strip()
        customer.email   = request.form.get('email', '').strip() or None
        customer.phone   = request.form.get('phone', '').strip() or None
        customer.company = request.form.get('company', '').strip() or None
        customer.address = request.form.get('address', '').strip() or None
        customer.notes   = request.form.get('notes', '').strip() or None
        customer.status  = request.form.get('status', 'lead')
        db.session.commit()
        log_activity(f"Updated customer {customer.name}", "Customer", customer.id, customer.name)
        flash('Customer updated')
        return redirect(url_for('customer_detail', cid=customer.id))
    return render_template('crm/customer_form.html', customer=customer)


@app.route('/customer/<int:cid>/delete', methods=['POST'])
@require_permission('delete_customer')
def customer_delete(cid):
    customer = Customer.query.get_or_404(cid)
    c_name = customer.name
    db.session.delete(customer)
    db.session.commit()
    log_activity(f"Deleted customer {c_name}", "Customer", cid, c_name)
    flash('Customer deleted')
    return redirect(url_for('customer_list'))


# ── Routes — Analytics & Alerts ───────────────────────────────
@app.route('/analytics')
@require_permission('analytics')
def analytics():
    invoices_all = Invoice.query.all()
    total_rev = sum(inv.total_amount for inv in invoices_all)
    total_invoices = len(invoices_all)
    total_customers = Customer.query.count()
    avg_invoice_val = (total_rev / total_invoices) if total_invoices > 0 else 0.0

    # Top 5 products by quantity sold
    top_prods_query = db.session.query(
        Product,
        db.func.sum(InvoiceItem.quantity).label('total_qty')
    ).join(InvoiceItem, Product.id == InvoiceItem.product_id)\
     .group_by(Product.id)\
     .order_by(db.desc('total_qty'))\
     .limit(5).all()

    max_qty = top_prods_query[0].total_qty if top_prods_query else 1
    top_products = []
    for prod, qty in top_prods_query:
        pct = int((qty / max_qty) * 100) if max_qty > 0 else 0
        top_products.append({
            "product": prod,
            "quantity_sold": qty,
            "percent": pct
        })

    # Monthly revenue for last 6 months
    monthly_labels = []
    monthly_revenue = []
    now = datetime.utcnow()
    for i in range(5, -1, -1):
        m_date = now - timedelta(days=i * 30)
        label = m_date.strftime('%b %Y')
        monthly_labels.append(label)
        invoices = Invoice.query.filter(
            db.extract('year', Invoice.created_at) == m_date.year,
            db.extract('month', Invoice.created_at) == m_date.month
        ).all()
        rev = sum(inv.total_amount for inv in invoices)
        monthly_revenue.append(round(rev, 2))

    # Top customers by total spend
    all_custs = Customer.query.all()
    all_custs = [c for c in all_custs if c.total_spent > 0]
    all_custs.sort(key=lambda c: c.total_spent, reverse=True)
    top_5_custs = all_custs[:5]
    max_spend = top_5_custs[0].total_spent if top_5_custs else 1.0
    top_customers = []
    for c in top_5_custs:
        pct = int((c.total_spent / max_spend) * 100) if max_spend > 0 else 0
        top_customers.append({
            "customer": c,
            "total_spent": c.total_spent,
            "percent": pct
        })

    return render_template('analytics.html',
                           total_revenue=total_rev,
                           total_invoices=total_invoices,
                           total_customers=total_customers,
                           avg_invoice=avg_invoice_val,
                           avg_invoice_value=avg_invoice_val,
                           top_products=top_products,
                           month_labels=monthly_labels,
                           month_data=monthly_revenue,
                           monthly_labels=monthly_labels,
                           monthly_revenue=monthly_revenue,
                           top_customers=top_customers)


@app.route('/alerts')
@login_required
def alerts():
    all_low = Product.query.filter(Product.quantity <= 10).order_by(Product.quantity).all()
    out_of_stock = [p for p in all_low if p.quantity == 0]
    critical     = [p for p in all_low if 1 <= p.quantity <= 5]
    low          = [p for p in all_low if 6 <= p.quantity <= 10]
    return render_template('alerts.html',
                           out_of_stock=out_of_stock,
                           critical=critical,
                           low=low)


# ── Routes — Activity Log ─────────────────────────────────────
@app.route('/activity')
@login_required
def activity_log():
    page = request.args.get('page', 1, type=int)
    pagination = ActivityLog.query.order_by(ActivityLog.timestamp.desc()).paginate(page=page, per_page=20, error_out=False)
    return render_template('activity.html', pagination=pagination)


# ── Routes — Global Search ────────────────────────────────────
@app.route('/search')
@login_required
def search():
    query = request.args.get('q', '').strip()
    if not query:
        return render_template('search.html', query='', products=[], customers=[], suppliers=[], invoices=[])

    q_lower = query.lower()

    # Search Products
    all_prods = Product.query.all()
    matched_products = [p for p in all_prods if q_lower in p.name.lower() or q_lower in p.sku.lower()]

    # Search Customers
    all_custs = Customer.query.all()
    matched_customers = [c for c in all_custs if q_lower in c.name.lower() or (c.email and q_lower in c.email.lower()) or (c.company and q_lower in c.company.lower())]

    # Search Suppliers
    all_sups = Supplier.query.all()
    matched_suppliers = [s for s in all_sups if q_lower in s.name.lower() or (s.email and q_lower in s.email.lower())]

    # Search Invoices
    all_invs = Invoice.query.all()
    matched_invoices = [inv for inv in all_invs if q_lower in inv.client_name.lower() or q_lower in f"inv-{inv.id:04d}".lower() or q_lower == str(inv.id)]

    return render_template('search.html',
                           query=query,
                           products=matched_products,
                           customers=matched_customers,
                           suppliers=matched_suppliers,
                           invoices=matched_invoices)


# ── Routes — Currencies ───────────────────────────────────────
@app.route('/settings/currencies')
@admin_required
def currency_list():
    currencies = Currency.query.order_by(Currency.id).all()
    return render_template('settings/currencies.html', currencies=currencies)


@app.route('/settings/currency/new', methods=['POST'])
@admin_required
def currency_create():
    code   = request.form.get('code', '').strip().upper()
    symbol = request.form.get('symbol', '').strip()
    name   = request.form.get('name', '').strip()
    rate   = float(request.form.get('exchange_rate') or 1.0)

    if not code or not symbol or not name:
        flash('All currency fields are required.', 'danger')
        return redirect(url_for('currency_list'))

    if Currency.query.filter_by(code=code).first():
        flash(f'Currency {code} already exists.', 'danger')
        return redirect(url_for('currency_list'))

    curr = Currency(code=code, symbol=symbol, name=name, exchange_rate=rate)
    db.session.add(curr)
    db.session.commit()
    log_activity(f"Created currency {curr.code}", "Currency", curr.id, curr.code)
    flash(f"Currency {curr.code} added successfully.")
    return redirect(url_for('currency_list'))


@app.route('/settings/currency/<int:cid>/edit', methods=['POST'])
@admin_required
def currency_edit(cid):
    curr = Currency.query.get_or_404(cid)
    curr.symbol = request.form.get('symbol', '').strip() or curr.symbol
    curr.name   = request.form.get('name', '').strip() or curr.name
    curr.exchange_rate = float(request.form.get('exchange_rate') or curr.exchange_rate)
    db.session.commit()
    log_activity(f"Updated currency {curr.code}", "Currency", curr.id, curr.code)
    flash(f"Currency {curr.code} updated.")
    return redirect(url_for('currency_list'))


@app.route('/settings/currency/<int:cid>/delete', methods=['POST'])
@admin_required
def currency_delete(cid):
    curr = Currency.query.get_or_404(cid)
    if curr.code == 'USD':
        flash('Cannot delete base USD currency.', 'danger')
        return redirect(url_for('currency_list'))
    c_code = curr.code
    db.session.delete(curr)
    db.session.commit()
    log_activity(f"Deleted currency {c_code}", "Currency", cid, c_code)
    flash(f"Currency {c_code} deleted.")
    return redirect(url_for('currency_list'))


# ── Routes — CSV Export ───────────────────────────────────────
@app.route('/export/products')
@require_permission('export')
def export_products():
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['ID', 'Name', 'SKU', 'Category', 'Quantity', 'Price', 'Supplier'])
    for p in Product.query.order_by(Product.name).all():
        cw.writerow([
            p.id, p.name, p.sku,
            p.category.name if p.category else '',
            p.quantity, f"{p.price:.2f}",
            p.supplier.name if p.supplier else ''
        ])
    output = si.getvalue()
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=products.csv"}
    )


@app.route('/export/invoices')
@require_permission('export')
def export_invoices():
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['Invoice ID', 'Client Name', 'Date', 'Currency', 'Subtotal', 'Discount %', 'Total Amount', 'Items Count'])
    for inv in Invoice.query.order_by(Invoice.created_at.desc()).all():
        cw.writerow([
            f"INV-{inv.id:04d}", inv.client_name,
            inv.created_at.strftime('%Y-%m-%d %H:%M'),
            inv.currency_code, f"{inv.subtotal:.2f}",
            f"{inv.discount_pct}%", f"{inv.total_amount:.2f}",
            len(inv.items)
        ])
    output = si.getvalue()
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=invoices.csv"}
    )


@app.route('/export/customers')
@require_permission('export')
def export_customers():
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['ID', 'Name', 'Company', 'Email', 'Phone', 'Status', 'Total Invoices', 'Total Spent'])
    for c in Customer.query.order_by(Customer.name).all():
        cw.writerow([
            c.id, c.name, c.company or '',
            c.email or '', c.phone or '', c.status,
            len(c.invoices), f"{c.total_spent:.2f}"
        ])
    output = si.getvalue()
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=customers.csv"}
    )


# ── Bootstrap DB, Crons & Run ──────────────────────────────────
scheduler = BackgroundScheduler()


def init_db():
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    with app.app_context():
        db.create_all()

        # Seed initial admin user if none exists
        if User.query.first() is None:
            admin_user = User(
                username='admin',
                email='admin@stockflow.local',
                role='admin',
                is_active=True
            )
            admin_user.set_password('admin123')
            db.session.add(admin_user)
            db.session.commit()
            print("Default admin created: admin / admin123")

        # Seed default currencies if none exist
        if Currency.query.first() is None:
            defaults = [
                Currency(code='USD', symbol='$', name='US Dollar', exchange_rate=1.0),
                Currency(code='EUR', symbol='€', name='Euro', exchange_rate=0.92),
                Currency(code='GBP', symbol='£', name='British Pound', exchange_rate=0.79),
                Currency(code='MAD', symbol='د.م.', name='Moroccan Dirham', exchange_rate=10.0),
                Currency(code='DZD', symbol='دج', name='Algerian Dinar', exchange_rate=134.0),
            ]
            db.session.add_all(defaults)
            db.session.commit()
            print("Default currencies seeded: USD, EUR, GBP, MAD, DZD")

        # Seed default warehouse if none exists
        default_wh = Warehouse.query.filter_by(is_default=True).first()
        if not default_wh:
            default_wh = Warehouse(name="Main Warehouse", location="Primary Headquarters", notes="Central storage hub.", is_default=True)
            db.session.add(default_wh)
            db.session.commit()
            print("Default warehouse seeded: Main Warehouse")

        # Synchronize existing products into default warehouse
        all_prods = Product.query.all()
        for p in all_prods:
            ws = WarehouseStock.query.filter_by(warehouse_id=default_wh.id, product_id=p.id).first()
            if not ws:
                db.session.add(WarehouseStock(warehouse_id=default_wh.id, product_id=p.id, quantity=p.quantity))
        db.session.commit()

        # Seed schedule config records
        for jid in JOBS_METADATA.keys():
            if not ScheduleConfig.query.filter_by(job_id=jid).first():
                db.session.add(ScheduleConfig(job_id=jid, enabled=True, last_status='Scheduled'))
        db.session.commit()


def start_scheduler():
    try:
        # Schedule Daily Low-Stock Digest at 08:00
        scheduler.add_job(
            func=lambda: app.app_context().push() or execute_job('daily_low_stock'),
            trigger='cron',
            hour=8,
            minute=0,
            id='daily_low_stock',
            replace_existing=True
        )
        # Schedule Weekly Sales Summary Monday 09:00
        scheduler.add_job(
            func=lambda: app.app_context().push() or execute_job('weekly_sales_summary'),
            trigger='cron',
            day_of_week='mon',
            hour=9,
            minute=0,
            id='weekly_sales_summary',
            replace_existing=True
        )
        scheduler.start()
        atexit.register(lambda: scheduler.shutdown(wait=False))
    except Exception as e:
        print("Scheduler startup exception:", e)


init_db()

if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
    start_scheduler()

if __name__ == '__main__':
    app.run(debug=True)
