import os
import uuid
import csv
import io
import json
from datetime import datetime
from functools import wraps

from flask import (Flask, render_template, request, redirect,
                   url_for, flash, send_file, abort, Response)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin, login_user,
                         logout_user, login_required, current_user)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from io import BytesIO

import qrcode
import barcode
from barcode.writer import ImageWriter


# ── App setup ──────────────────────────────────────────────────
app = Flask(__name__)
app.config['SECRET_KEY'] = 'stockflow-secret-key-change-in-production-2026'
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{os.path.join(basedir, 'inventory.db')}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join(basedir, 'static', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5 MB max

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

db = SQLAlchemy(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message_category = 'info'


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def save_upload(file):
    """Save an uploaded file and return the stored filename."""
    if file and file.filename and allowed_file(file.filename):
        ext = file.filename.rsplit('.', 1)[1].lower()
        filename = f"{uuid.uuid4().hex}.{ext}"
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        return filename
    return None


def admin_required(f):
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            abort(403)
        return f(*args, **kwargs)
    return decorated_function


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
    role          = db.Column(db.String(20), default='staff')  # admin, manager, staff
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
    code          = db.Column(db.String(10), unique=True, nullable=False)   # USD, EUR, etc.
    symbol        = db.Column(db.String(10), nullable=False)               # $, €, etc.
    name          = db.Column(db.String(50), nullable=False)               # US Dollar
    exchange_rate = db.Column(db.Float, default=1.0)                       # Relative to USD baseline

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


class Product(db.Model):
    __tablename__ = "product"
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(120), nullable=False)
    sku         = db.Column(db.String(80),  unique=True, nullable=False)
    quantity    = db.Column(db.Integer, default=0)
    price       = db.Column(db.Float, nullable=False)
    image       = db.Column(db.String(200), nullable=True)   # filename in uploads/
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
    status     = db.Column(db.String(20), default='lead')   # lead / active / inactive
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
    items        = db.relationship('InvoiceItem', backref='invoice',
                                   lazy=True, cascade="all, delete-orphan")
    returns      = db.relationship('Return', backref='invoice',
                                   lazy=True, cascade="all, delete-orphan")

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
    status      = db.Column(db.String(20), default='draft')  # draft / sent / received
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    notes       = db.Column(db.Text, nullable=True)
    items       = db.relationship('POItem', backref='purchase_order',
                                  lazy=True, cascade="all, delete-orphan")

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
    status        = db.Column(db.String(20), default='pending')  # pending / approved / rejected
    items         = db.relationship('ReturnItem', backref='return_record',
                                    lazy=True, cascade="all, delete-orphan")

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
    """Helper to log an activity event."""
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
    except Exception as e:
        db.session.rollback()


# ── Context Processors ─────────────────────────────────────────

@app.context_processor
def inject_global_data():
    try:
        low_stock_count = Product.query.filter(Product.quantity <= 10).count()
    except Exception:
        low_stock_count = 0
    return dict(low_stock_count=low_stock_count)


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

        # Prevent admin removing their own admin status or deactivating self
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
@login_required
def product_create():
    categories = Category.query.order_by(Category.name).all()
    suppliers  = Supplier.query.order_by(Supplier.name).all()
    all_tags   = Tag.query.order_by(Tag.name).all()

    if request.method == 'POST':
        image_file  = request.files.get('image')
        cat_id      = request.form.get('category_id')
        sup_id      = request.form.get('supplier_id')
        tag_ids     = request.form.getlist('tag_ids')

        product = Product(
            name        = request.form['name'].strip(),
            sku         = request.form['sku'].strip(),
            quantity    = int(request.form.get('quantity') or 0),
            price       = float(request.form.get('price') or 0),
            category_id = int(cat_id) if cat_id else None,
            supplier_id = int(sup_id) if sup_id else None,
            image       = save_upload(image_file),
        )

        if tag_ids:
            product.tags = Tag.query.filter(Tag.id.in_([int(tid) for tid in tag_ids])).all()

        db.session.add(product)
        db.session.commit()
        log_activity(f"Created product {product.name}", "Product", product.id, product.name)
        flash('Product added successfully')
        return redirect(url_for('index'))

    return render_template('product_form.html', product=None,
                           categories=categories, suppliers=suppliers, all_tags=all_tags)


@app.route('/product/<int:pid>/edit', methods=['GET', 'POST'])
@login_required
def product_edit(pid):
    product    = Product.query.get_or_404(pid)
    categories = Category.query.order_by(Category.name).all()
    suppliers  = Supplier.query.order_by(Supplier.name).all()
    all_tags   = Tag.query.order_by(Tag.name).all()

    if request.method == 'POST':
        product.name     = request.form['name'].strip()
        product.sku      = request.form['sku'].strip()
        product.quantity = int(request.form.get('quantity') or 0)
        product.price    = float(request.form.get('price') or 0)
        cat_id           = request.form.get('category_id')
        sup_id           = request.form.get('supplier_id')
        tag_ids          = request.form.getlist('tag_ids')

        product.category_id = int(cat_id) if cat_id else None
        product.supplier_id = int(sup_id) if sup_id else None

        # Clear and re-add tags
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
@login_required
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
    old_qty = product.quantity
    product.quantity = max(0, product.quantity + adjustment)
    db.session.commit()
    sign = '+' if adjustment > 0 else ''
    log_activity(f"Adjusted stock for {product.name} ({sign}{adjustment} -> {product.quantity})",
                 "Product", product.id, product.name)
    flash(f"Stock adjusted for {product.name} ({sign}{adjustment} → new quantity: {product.quantity})")
    return redirect(request.referrer or url_for('alerts'))


# ── Feature 2 — QR Code & Barcode Routes ──────────────────────

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
@login_required
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
@login_required
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
@login_required
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


# ── Feature 7 — Tags ──────────────────────────────────────────

@app.route('/tags')
@login_required
def tag_list():
    tags = Tag.query.order_by(Tag.name).all()
    return render_template('tags.html', tags=tags)


@app.route('/tag/new', methods=['GET', 'POST'])
@login_required
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
@login_required
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
@login_required
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
@login_required
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
        return redirect(url_for('supplier_list'))
    return render_template('supplier_form.html', supplier=None)


@app.route('/supplier/<int:sid>')
@login_required
def supplier_detail(sid):
    supplier = Supplier.query.get_or_404(sid)
    return render_template('supplier_detail.html', supplier=supplier)


@app.route('/supplier/<int:sid>/edit', methods=['GET', 'POST'])
@login_required
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
@login_required
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


# ── Feature 3 — Purchase Orders ───────────────────────────────

@app.route('/purchase-orders')
@login_required
def purchase_order_list():
    pos = PurchaseOrder.query.order_by(PurchaseOrder.created_at.desc()).all()
    return render_template('purchase_orders.html', purchase_orders=pos)


@app.route('/purchase-order/new', methods=['GET', 'POST'])
@login_required
def purchase_order_create():
    suppliers = Supplier.query.order_by(Supplier.name).all()
    products  = Product.query.order_by(Product.name).all()
    preselected_sup = request.args.get('supplier_id', type=int)

    if request.method == 'POST':
        sup_id = request.form.get('supplier_id')
        notes  = request.form.get('notes', '').strip() or None
        status = request.form.get('status', 'draft')

        if not sup_id:
            flash('Supplier is required.', 'danger')
            return redirect(url_for('purchase_order_create'))

        po = PurchaseOrder(
            supplier_id = int(sup_id),
            notes       = notes,
            status      = status
        )
        db.session.add(po)
        db.session.flush()

        product_ids = request.form.getlist('product_id')
        quantities  = request.form.getlist('quantity')
        unit_costs  = request.form.getlist('unit_cost')

        for pid, qty, cost in zip(product_ids, quantities, unit_costs):
            if not pid or not qty or int(qty) <= 0:
                continue
            item = POItem(
                po_id      = po.id,
                product_id = int(pid),
                quantity   = int(qty),
                unit_cost  = float(cost or 0.0)
            )
            db.session.add(item)

        # If created directly as received, restock immediately
        if po.status == 'received':
            for item in po.items:
                if item.product:
                    item.product.quantity += item.quantity

        db.session.commit()
        log_activity(f"Created purchase order PO-{po.id:04d}", "PurchaseOrder", po.id, f"PO-{po.id:04d}")
        flash('Purchase Order created successfully.')
        return redirect(url_for('purchase_order_detail', poid=po.id))

    return render_template('purchase_order_form.html',
                           suppliers=suppliers, products=products, preselected_sup=preselected_sup)


@app.route('/purchase-order/<int:poid>')
@login_required
def purchase_order_detail(poid):
    po = PurchaseOrder.query.get_or_404(poid)
    return render_template('purchase_order_detail.html', po=po)


@app.route('/purchase-order/<int:poid>/receive', methods=['POST'])
@login_required
def purchase_order_receive(poid):
    po = PurchaseOrder.query.get_or_404(poid)
    if po.status == 'received':
        flash('Purchase order was already received.', 'info')
        return redirect(url_for('purchase_order_detail', poid=po.id))

    po.status = 'received'
    for item in po.items:
        if item.product:
            item.product.quantity += item.quantity

    db.session.commit()
    log_activity(f"Received purchase order PO-{po.id:04d} (inventory restocked)",
                 "PurchaseOrder", po.id, f"PO-{po.id:04d}")
    flash(f"Purchase Order PO-{po.id:04d} marked as received. Stock quantities have been updated!")
    return redirect(url_for('purchase_order_detail', poid=po.id))


@app.route('/purchase-order/<int:poid>/delete', methods=['POST'])
@login_required
def purchase_order_delete(poid):
    po = PurchaseOrder.query.get_or_404(poid)
    po_num = f"PO-{po.id:04d}"
    db.session.delete(po)
    db.session.commit()
    log_activity(f"Deleted purchase order {po_num}", "PurchaseOrder", poid, po_num)
    flash(f"{po_num} deleted successfully.")
    return redirect(url_for('purchase_order_list'))


# ── Feature 4 — Global Search ─────────────────────────────────

@app.route('/search')
@login_required
def search():
    query = request.args.get('q', '').strip().lower()

    if not query:
        return render_template('search.html', query='', results=None)

    # Python-side filtering for robust cross-field matching
    products = [
        p for p in Product.query.all()
        if query in (p.name or '').lower() or query in (p.sku or '').lower()
    ]

    customers = [
        c for c in Customer.query.all()
        if query in (c.name or '').lower()
        or query in (c.email or '').lower()
        or query in (c.company or '').lower()
    ]

    suppliers = [
        s for s in Supplier.query.all()
        if query in (s.name or '').lower()
        or query in (s.email or '').lower()
    ]

    invoices = [
        inv for inv in Invoice.query.all()
        if query in (inv.client_name or '').lower()
        or query in f"inv-{inv.id:04d}"
        or query == str(inv.id)
    ]

    results = {
        'products': products,
        'customers': customers,
        'suppliers': suppliers,
        'invoices': invoices,
        'total': len(products) + len(customers) + len(suppliers) + len(invoices)
    }

    return render_template('search.html', query=request.args.get('q', ''), results=results)


# ── Feature 5 — Activity Log / Audit Trail ────────────────────

@app.route('/activity')
@login_required
def activity_log():
    page = request.args.get('page', 1, type=int)
    pagination = ActivityLog.query.order_by(ActivityLog.timestamp.desc()).paginate(
        page=page, per_page=20, error_out=False
    )
    return render_template('activity.html', pagination=pagination)


# ── Feature 6 — Multi-Currency Settings ───────────────────────

@app.route('/settings/currencies')
@login_required
def currency_list():
    currencies = Currency.query.order_by(Currency.id).all()
    return render_template('settings/currencies.html', currencies=currencies)


@app.route('/settings/currency/new', methods=['POST'])
@login_required
def currency_create():
    code   = request.form.get('code', '').strip().upper()
    symbol = request.form.get('symbol', '').strip()
    name   = request.form.get('name', '').strip()
    rate   = request.form.get('exchange_rate', '1.0').strip()

    if not code or not symbol or not name:
        flash('Code, symbol, and name are required.', 'danger')
        return redirect(url_for('currency_list'))

    if Currency.query.filter_by(code=code).first():
        flash(f"Currency {code} already exists.", 'danger')
        return redirect(url_for('currency_list'))

    try:
        exchange_rate = float(rate)
    except ValueError:
        exchange_rate = 1.0

    curr = Currency(code=code, symbol=symbol, name=name, exchange_rate=exchange_rate)
    db.session.add(curr)
    db.session.commit()
    log_activity(f"Added currency {curr.code} ({curr.symbol})", "Currency", curr.id, curr.code)
    flash(f"Currency {curr.code} added successfully.")
    return redirect(url_for('currency_list'))


@app.route('/settings/currency/<int:cid>/edit', methods=['POST'])
@login_required
def currency_edit(cid):
    curr = Currency.query.get_or_404(cid)
    curr.name   = request.form.get('name', curr.name).strip()
    curr.symbol = request.form.get('symbol', curr.symbol).strip()
    try:
        curr.exchange_rate = float(request.form.get('exchange_rate', curr.exchange_rate))
    except ValueError:
        pass
    db.session.commit()
    log_activity(f"Updated currency {curr.code} (rate: {curr.exchange_rate})", "Currency", curr.id, curr.code)
    flash(f"Currency {curr.code} updated.")
    return redirect(url_for('currency_list'))


@app.route('/settings/currency/<int:cid>/delete', methods=['POST'])
@login_required
def currency_delete(cid):
    curr = Currency.query.get_or_404(cid)
    if curr.code == 'USD':
        flash('Base currency USD cannot be deleted.', 'danger')
        return redirect(url_for('currency_list'))
    code = curr.code
    db.session.delete(curr)
    db.session.commit()
    log_activity(f"Deleted currency {code}", "Currency", cid, code)
    flash(f"Currency {code} deleted.")
    return redirect(url_for('currency_list'))


# ── Routes — Low Stock Alerts ─────────────────────────────────

@app.route('/alerts')
@login_required
def alerts():
    low_products = Product.query.filter(Product.quantity <= 10).order_by(Product.quantity.asc()).all()
    out_of_stock = [p for p in low_products if p.quantity == 0]
    critical     = [p for p in low_products if 1 <= p.quantity <= 5]
    low          = [p for p in low_products if 6 <= p.quantity <= 10]
    return render_template('alerts.html',
                           out_of_stock=out_of_stock,
                           critical=critical,
                           low=low,
                           total_alerts=len(low_products))


# ── Routes — Analytics ────────────────────────────────────────

@app.route('/analytics')
@login_required
def analytics():
    all_invoices  = Invoice.query.all()
    all_customers = Customer.query.all()

    total_revenue   = sum(inv.total_amount for inv in all_invoices)
    total_invoices  = len(all_invoices)
    total_customers = len(all_customers)
    avg_invoice     = (total_revenue / total_invoices) if total_invoices > 0 else 0.0

    # Top 5 products by quantity sold
    product_sales = {}
    for item in InvoiceItem.query.all():
        if item.product:
            product_sales[item.product] = product_sales.get(item.product, 0) + item.quantity
    top_products_sorted = sorted(product_sales.items(), key=lambda x: x[1], reverse=True)[:5]
    max_sold = top_products_sorted[0][1] if top_products_sorted else 1
    top_products = [
        {
            'product': p,
            'quantity_sold': qty,
            'percent': int(round((qty / max_sold) * 100)) if max_sold else 0
        }
        for p, qty in top_products_sorted
    ]

    # Monthly revenue (last 6 months in Python)
    now = datetime.utcnow()
    month_keys = []
    for i in range(5, -1, -1):
        m = now.month - i
        y = now.year
        while m <= 0:
            m += 12
            y -= 1
        month_keys.append((y, m))

    month_labels = [datetime(y, m, 1).strftime('%b %Y') for y, m in month_keys]
    month_data = [0.0] * 6
    for inv in all_invoices:
        if inv.created_at:
            key = (inv.created_at.year, inv.created_at.month)
            if key in month_keys:
                idx = month_keys.index(key)
                month_data[idx] += inv.total_amount
    month_data = [round(val, 2) for val in month_data]

    # Revenue by Customer (Top 5)
    cust_spending = []
    for c in all_customers:
        spent = c.total_spent
        if spent > 0:
            cust_spending.append((c, spent))
    cust_spending.sort(key=lambda x: x[1], reverse=True)
    top_customers_sorted = cust_spending[:5]
    max_spent = top_customers_sorted[0][1] if top_customers_sorted else 1.0
    top_customers = [
        {
            'customer': c,
            'total_spent': spent,
            'percent': int(round((spent / max_spent) * 100)) if max_spent else 0
        }
        for c, spent in top_customers_sorted
    ]

    return render_template('analytics.html',
                           total_revenue=total_revenue,
                           total_invoices=total_invoices,
                           total_customers=total_customers,
                           avg_invoice=avg_invoice,
                           top_products=top_products,
                           month_labels=month_labels,
                           month_data=month_data,
                           top_customers=top_customers)


# ── Routes — CSV Export ───────────────────────────────────────

@app.route('/export/products')
@login_required
def export_products():
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['name', 'sku', 'category', 'quantity', 'price'])
    products = Product.query.order_by(Product.name).all()
    for p in products:
        cw.writerow([
            p.name,
            p.sku,
            p.category.name if p.category else '',
            p.quantity,
            f"{p.price:.2f}"
        ])
    return Response(
        si.getvalue(),
        mimetype='text/csv',
        headers={"Content-Disposition": "attachment; filename=products.csv"}
    )


@app.route('/export/invoices')
@login_required
def export_invoices():
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['id', 'client', 'date', 'currency', 'items_count', 'total'])
    invoices = Invoice.query.order_by(Invoice.created_at.desc()).all()
    for inv in invoices:
        cw.writerow([
            f"INV-{inv.id:04d}",
            inv.client_name,
            inv.created_at.strftime('%Y-%m-%d %H:%M') if inv.created_at else '',
            inv.currency_code,
            len(inv.items),
            f"{inv.total_amount:.2f}"
        ])
    return Response(
        si.getvalue(),
        mimetype='text/csv',
        headers={"Content-Disposition": "attachment; filename=invoices.csv"}
    )


@app.route('/export/customers')
@login_required
def export_customers():
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['name', 'company', 'email', 'phone', 'status', 'total_spent'])
    customers = Customer.query.order_by(Customer.name).all()
    for c in customers:
        cw.writerow([
            c.name,
            c.company or '',
            c.email or '',
            c.phone or '',
            c.status or '',
            f"{c.total_spent:.2f}"
        ])
    return Response(
        si.getvalue(),
        mimetype='text/csv',
        headers={"Content-Disposition": "attachment; filename=customers.csv"}
    )


# ── Routes — Invoices ─────────────────────────────────────────

@app.route('/invoices')
@login_required
def invoice_list():
    invoices = Invoice.query.order_by(Invoice.created_at.desc()).all()
    return render_template('invoice_list.html', invoices=invoices)


@app.route('/invoice/new', methods=['GET', 'POST'])
@login_required
def invoice_create():
    products   = Product.query.all()
    customers  = Customer.query.order_by(Customer.name).all()
    currencies = Currency.query.order_by(Currency.id).all()

    if request.method == 'POST':
        customer_id  = request.form.get('customer_id')
        currency_id  = request.form.get('currency_id')
        customer     = Customer.query.get(int(customer_id)) if customer_id else None
        client_name  = customer.name if customer else (request.form.get('client_name', '').strip() or 'Walk-in')
        try:
            discount_pct = float(request.form.get('discount_pct') or 0.0)
        except ValueError:
            discount_pct = 0.0
        discount_pct = max(0.0, min(100.0, discount_pct))

        invoice = Invoice(
            client_name  = client_name,
            customer_id  = customer.id if customer else None,
            currency_id  = int(currency_id) if currency_id else None,
            discount_pct = discount_pct
        )
        db.session.add(invoice)
        db.session.flush()

        product_ids = request.form.getlist('product_id')
        quantities  = request.form.getlist('quantity')
        for pid, qty in zip(product_ids, quantities):
            if not qty or int(qty) == 0:
                continue
            product = Product.query.get(int(pid))
            if product is None:
                continue
            item = InvoiceItem(
                invoice_id = invoice.id,
                product_id = product.id,
                quantity   = int(qty),
                unit_price = product.price,
            )
            db.session.add(item)
            product.quantity = max(product.quantity - int(qty), 0)

        db.session.commit()
        log_activity(f"Created invoice INV-{invoice.id:04d} for {invoice.client_name}",
                     "Invoice", invoice.id, f"INV-{invoice.id:04d}")
        flash('Invoice created')
        return redirect(url_for('invoice_view', invoice_id=invoice.id))

    return render_template('invoice_form.html',
                           products=products, customers=customers, currencies=currencies)


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


# ── Feature 8 — Returns / Refunds ─────────────────────────────

@app.route('/returns')
@login_required
def return_list():
    returns = Return.query.order_by(Return.created_at.desc()).all()
    return render_template('returns.html', returns=returns)


@app.route('/invoice/<int:invoice_id>/return', methods=['GET', 'POST'])
@login_required
def invoice_return_create(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)

    if request.method == 'POST':
        reason = request.form.get('reason', '').strip()
        if not reason:
            flash('Return reason is required.', 'danger')
            return render_template('return_form.html', invoice=invoice)

        item_ids   = request.form.getlist('item_id')
        quantities = request.form.getlist('quantity')

        return_record = Return(
            invoice_id = invoice.id,
            reason     = reason,
            status     = 'pending'
        )
        db.session.add(return_record)
        db.session.flush()

        total_refund = 0.0
        for iid, qty in zip(item_ids, quantities):
            q = int(qty or 0)
            if q <= 0:
                continue
            inv_item = InvoiceItem.query.get(int(iid))
            if inv_item and inv_item.invoice_id == invoice.id:
                # clamp quantity to invoiced quantity
                q = min(q, inv_item.quantity)
                ritem = ReturnItem(
                    return_id  = return_record.id,
                    product_id = inv_item.product_id,
                    quantity   = q,
                    unit_price = inv_item.unit_price
                )
                db.session.add(ritem)
                total_refund += q * inv_item.unit_price

        # factor in invoice discount if any
        if invoice.discount_pct and invoice.discount_pct > 0:
            total_refund = total_refund * (1.0 - (invoice.discount_pct / 100.0))

        return_record.refund_amount = round(total_refund, 2)
        db.session.commit()
        log_activity(f"Requested return for INV-{invoice.id:04d} (Refund: {invoice.currency_symbol}{return_record.refund_amount:.2f})",
                     "Return", return_record.id, f"Return #{return_record.id}")
        flash('Return request submitted.')
        return redirect(url_for('return_list'))

    return render_template('return_form.html', invoice=invoice)


@app.route('/return/<int:rid>/approve', methods=['POST'])
@login_required
def return_approve(rid):
    ret = Return.query.get_or_404(rid)
    if ret.status == 'approved':
        flash('Return is already approved.', 'info')
        return redirect(url_for('return_list'))

    ret.status = 'approved'
    # Restock returned items
    for item in ret.items:
        if item.product:
            item.product.quantity += item.quantity

    db.session.commit()
    log_activity(f"Approved return #{ret.id} for INV-{ret.invoice_id:04d} (Items restocked)",
                 "Return", ret.id, f"Return #{ret.id}")
    flash(f"Return #{ret.id} approved. Items have been returned to stock.")
    return redirect(url_for('return_list'))


@app.route('/return/<int:rid>/reject', methods=['POST'])
@login_required
def return_reject(rid):
    ret = Return.query.get_or_404(rid)
    ret.status = 'rejected'
    db.session.commit()
    log_activity(f"Rejected return #{ret.id} for INV-{ret.invoice_id:04d}",
                 "Return", ret.id, f"Return #{ret.id}")
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
@login_required
def customer_create():
    if request.method == 'POST':
        customer = Customer(
            name    = request.form['name'].strip(),
            email   = request.form.get('email', '').strip() or None,
            phone   = request.form.get('phone', '').strip() or None,
            company = request.form.get('company', '').strip() or None,
            address = request.form.get('address', '').strip() or None,
            notes   = request.form.get('notes', '').strip() or None,
            status  = request.form.get('status', 'lead'),
        )
        db.session.add(customer)
        db.session.commit()
        log_activity(f"Created customer {customer.name}", "Customer", customer.id, customer.name)
        flash('Customer added')
        return redirect(url_for('customer_detail', cid=customer.id))
    return render_template('crm/customer_form.html', customer=None)


@app.route('/customer/<int:cid>')
@login_required
def customer_detail(cid):
    customer = Customer.query.get_or_404(cid)
    return render_template('crm/customer_detail.html', customer=customer)


@app.route('/customer/<int:cid>/edit', methods=['GET', 'POST'])
@login_required
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
@login_required
def customer_delete(cid):
    customer = Customer.query.get_or_404(cid)
    c_name = customer.name
    db.session.delete(customer)
    db.session.commit()
    log_activity(f"Deleted customer {c_name}", "Customer", cid, c_name)
    flash('Customer deleted')
    return redirect(url_for('customer_list'))


# ── Bootstrap DB & Run ────────────────────────────────────────

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


init_db()

if __name__ == '__main__':
    app.run(debug=True)
