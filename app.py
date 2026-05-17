import os, random, string, json
from datetime import datetime
from functools import wraps
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, jsonify, abort
)
from flask_login import (
    LoginManager, login_user, logout_user,
    login_required, current_user, UserMixin
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import requests

load_dotenv()

app = Flask(__name__)

# ---------- Database ----------
# Vercel PostgreSQL will provide DATABASE_URL automatically.
# If not present (local dev), fallback to a writable SQLite in /tmp (NOT for production)
DATABASE_URL = os.getenv('DATABASE_URL')
if not DATABASE_URL:
    import tempfile
    db_path = os.path.join(tempfile.gettempdir(), 'database.db')
    DATABASE_URL = f'sqlite:///{db_path}'
    print("WARNING: Using ephemeral SQLite. Data will be lost on each deploy.")

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-key')

# Extra settings for PostgreSQL
if DATABASE_URL.startswith('postgresql://'):
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'connect_args': {'options': '-c timezone=utc'}
    }

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'សូមចូលគណនីដើម្បីបន្ត។'

BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID')

# ------------------------- Models -------------------------
class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    balance = db.Column(db.Float, default=0.0)
    api_key = db.Column(db.String(128), unique=True, nullable=True)
    is_admin = db.Column(db.Boolean, default=False)
    is_banned = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def is_active(self):
        return not self.is_banned

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Deposit(db.Model):
    __tablename__ = 'deposits'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), default='pending')
    telegram_message_id = db.Column(db.Integer, nullable=True)
    telegram_chat_id = db.Column(db.String(50), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Order(db.Model):
    __tablename__ = 'orders'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    service_id = db.Column(db.Integer, db.ForeignKey('services.id'), nullable=True)
    game = db.Column(db.String(10), nullable=False)
    uid = db.Column(db.String(50), nullable=False)
    server_id = db.Column(db.String(50), nullable=True)
    product_name = db.Column(db.String(50), nullable=False)
    price = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), default='pending')
    command_text = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Service(db.Model):
    __tablename__ = 'services'
    id = db.Column(db.Integer, primary_key=True)
    game = db.Column(db.String(10), nullable=False)
    product_name = db.Column(db.String(50), nullable=False)
    supplier_price = db.Column(db.Float, nullable=False)
    selling_price = db.Column(db.Float, nullable=False)
    command_format = db.Column(db.String(100), nullable=False)
    requires_server_id = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)

# ------------------------- Helper functions -------------------------
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def generate_csrf_token():
    if '_csrf_token' not in session:
        session['_csrf_token'] = ''.join(random.choices(string.ascii_letters + string.digits, k=32))
    return session['_csrf_token']

def csrf_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.method == 'POST':
            token = request.form.get('_csrf_token')
            if not token or token != session.get('_csrf_token'):
                abort(403, description="CSRF validation failed")
        return f(*args, **kwargs)
    return decorated_function

app.jinja_env.globals['csrf_token'] = generate_csrf_token

def admin_required(f):
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated_function

def send_telegram(chat_id, text, reply_markup=None):
    if not BOT_TOKEN:
        return None
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}
    if reply_markup:
        payload['reply_markup'] = json.dumps(reply_markup)
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.json()
    except Exception as e:
        print('Telegram error:', e)
        return None

def edit_telegram_message(chat_id, message_id, text, reply_markup=None):
    if not BOT_TOKEN:
        return
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/editMessageText'
    payload = {'chat_id': chat_id, 'message_id': message_id, 'text': text, 'parse_mode': 'HTML'}
    if reply_markup:
        payload['reply_markup'] = json.dumps(reply_markup)
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print('Telegram edit error:', e)

def answer_callback(callback_id, text=""):
    if not BOT_TOKEN:
        return
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery'
    payload = {'callback_query_id': callback_id, 'text': text}
    requests.post(url, json=payload, timeout=10)

def apply_markup(supplier_price):
    if supplier_price < 1.0:
        margin = 0.06
    elif supplier_price < 10.0:
        margin = 0.04
    else:
        margin = 0.03
    return round(supplier_price * (1 + margin), 2)

# ---------- Your full SERVICES_DATA (copy your original list here) ----------
SERVICES_DATA = [
    ("ff", "25", 0.24, "/ff {uid} 25", False),
    ("ff", "100", 0.84, "/ff {uid} 100", False),
    # ... (add all your services – I'm truncating for brevity, but you must paste the complete list)
]

def populate_services():
    if Service.query.first() is None:
        for game, product, supplier, cmd, needs_server in SERVICES_DATA:
            selling = apply_markup(supplier)
            s = Service(
                game=game, product_name=product,
                supplier_price=supplier, selling_price=selling,
                command_format=cmd, requires_server_id=needs_server
            )
            db.session.add(s)
        db.session.commit()

# ------------------------- Routes (all your original endpoints) -------------------------
@app.route('/')
def index():
    services = Service.query.filter_by(is_active=True).all()
    return render_template('index.html', services=services)

@app.route('/register', methods=['GET', 'POST'])
@csrf_required
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        confirm = request.form.get('confirm_password', '').strip()
        if not username or not email or not password:
            flash('សូមបំពេញគ្រប់វាល។', 'danger')
            return redirect(url_for('register'))
        if password != confirm:
            flash('ពាក្យសម្ងាត់មិនត្រូវគ្នា។', 'danger')
            return redirect(url_for('register'))
        if User.query.filter_by(username=username).first() or User.query.filter_by(email=email).first():
            flash('ឈ្មោះអ្នកប្រើ ឬអ៊ីមែលមានរួចហើយ។', 'danger')
            return redirect(url_for('register'))
        user = User(username=username, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        send_telegram(ADMIN_CHAT_ID, f"🆕 អ្នកប្រើថ្មីបានចុះឈ្មោះ\n👤 <b>{username}</b>\n📧 {email}")
        login_user(user)
        flash('ចុះឈ្មោះជោគជ័យ!', 'success')
        return redirect(url_for('dashboard'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
@csrf_required
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        remember = request.form.get('remember') == 'on'
        user = User.query.filter_by(username=username).first()
        if not user or not user.check_password(password):
            flash('ឈ្មោះអ្នកប្រើ ឬពាក្យសម្ងាត់មិនត្រឹមត្រូវ។', 'danger')
            return redirect(url_for('login'))
        if user.is_banned:
            flash('គណនីរបស់អ្នកត្រូវបានហាមឃាត់។', 'danger')
            return redirect(url_for('login'))
        login_user(user, remember=remember)
        flash('ចូលគណនីជោគជ័យ!', 'success')
        return redirect(url_for('dashboard'))
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('អ្នកបានចាកចេញដោយជោគជ័យ។', 'info')
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    recent_orders = Order.query.filter_by(user_id=current_user.id).order_by(Order.id.desc()).limit(5).all()
    deposits = Deposit.query.filter_by(user_id=current_user.id).order_by(Deposit.id.desc()).limit(5).all()
    total_orders = Order.query.filter_by(user_id=current_user.id).count()
    return render_template('dashboard.html',
                           recent_orders=recent_orders,
                           deposits=deposits,
                           total_orders=total_orders)

@app.route('/deposit', methods=['GET', 'POST'])
@login_required
@csrf_required
def deposit():
    if request.method == 'POST':
        amount = request.form.get('amount', '').strip()
        try:
            amount = float(amount)
            if amount <= 0:
                raise ValueError
        except:
            flash('ចំនួនទឹកប្រាក់មិនត្រឹមត្រូវ។', 'danger')
            return redirect(url_for('deposit'))
        dep = Deposit(user_id=current_user.id, amount=amount)
        db.session.add(dep)
        db.session.commit()
        inline_keyboard = {
            "inline_keyboard": [[
                {"text": "✅ ទទួល (Accept)", "callback_data": f"accept_{dep.id}"},
                {"text": "❌ បដិសេធ (Reject)", "callback_data": f"reject_{dep.id}"}
            ]]
        }
        msg = send_telegram(
            ADMIN_CHAT_ID,
            f"💰 <b>ប្រាក់តម្កល់ថ្មី</b>\n👤 {current_user.username}\n💵 ${amount:.2f}\n📌 កំពុងរងចាំ",
            reply_markup=inline_keyboard
        )
        if msg and msg.get('ok'):
            dep.telegram_message_id = msg['result']['message_id']
            dep.telegram_chat_id = str(msg['result']['chat']['id'])
            db.session.commit()
        flash('សំណើតម្កល់ប្រាក់បានដាក់ស្នើ។ សូមរង់ចាំ Admin យល់ព្រម។', 'info')
        return redirect(url_for('dashboard'))
    return render_template('deposit.html')

@app.route('/generate_api_key', methods=['POST'])
@login_required
@csrf_required
def generate_api_key():
    if current_user.api_key:
        flash('អ្នកមាន API Key រួចហើយ។ អាចកំណត់ឡើងវិញ (Reset) បាន។', 'warning')
        return redirect(url_for('dashboard'))
    key = 'api_sk_' + ''.join(random.choices(string.ascii_letters + string.digits, k=32))
    while User.query.filter_by(api_key=key).first():
        key = 'api_sk_' + ''.join(random.choices(string.ascii_letters + string.digits, k=32))
    current_user.api_key = key
    db.session.commit()
    flash('API Key បានបង្កើតដោយជោគជ័យ!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/reset_api_key', methods=['POST'])
@login_required
@csrf_required
def reset_api_key():
    key = 'api_sk_' + ''.join(random.choices(string.ascii_letters + string.digits, k=32))
    while User.query.filter_by(api_key=key).first():
        key = 'api_sk_' + ''.join(random.choices(string.ascii_letters + string.digits, k=32))
    current_user.api_key = key
    db.session.commit()
    flash('API Key បានកំណត់ឡើងវិញដោយជោគជ័យ។', 'success')
    return redirect(url_for('dashboard'))

@app.route('/api/docs')
def api_docs():
    return render_template('api_docs.html')

@app.route('/order_history')
@login_required
def order_history():
    orders = Order.query.filter_by(user_id=current_user.id).order_by(Order.id.desc()).all()
    return render_template('order_history.html', orders=orders)

@app.route('/api/order', methods=['POST'])
def api_order():
    api_key = request.headers.get('Authorization')
    if not api_key:
        return jsonify({"status": "error", "message": "Missing API key"}), 401
    user = User.query.filter_by(api_key=api_key).first()
    if not user or user.is_banned:
        return jsonify({"status": "error", "message": "Invalid API key or account banned"}), 401
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"status": "error", "message": "Invalid JSON"}), 400
    game = data.get('service', '').strip().lower()
    uid = str(data.get('uid', '')).strip()
    product = data.get('product', '').strip()
    server_id = str(data.get('server_id', '')).strip() if 'server_id' in data else None
    if not game or not uid or not product:
        return jsonify({"status": "error", "message": "Missing service/uid/product"}), 400
    if game in ['mg', 'mc'] and not server_id:
        return jsonify({"status": "error", "message": "Server ID required for this game"}), 400
    service = Service.query.filter_by(game=game, product_name=product, is_active=True).first()
    if not service:
        return jsonify({"status": "error", "message": "Service not found"}), 404
    price = service.selling_price
    if user.balance < price:
        return jsonify({"status": "error", "message": "Insufficient balance"}), 402
    if service.requires_server_id:
        cmd = service.command_format.format(uid=uid, server_id=server_id, product_name=product)
    else:
        cmd = service.command_format.format(uid=uid, product_name=product)
    user.balance -= price
    order = Order(
        user_id=user.id, service_id=service.id,
        game=game, uid=uid, server_id=server_id,
        product_name=product, price=price,
        status='pending', command_text=cmd
    )
    db.session.add(order)
    db.session.commit()
    send_telegram(
        ADMIN_CHAT_ID,
        f"🛒 <b>បញ្ជាទិញថ្មី</b>\n👤 {user.username}\n🎮 {game.upper()} | {product}\n🆔 {uid}"
        + (f" | 🌐 {server_id}" if server_id else "") + f"\n💰 ${price:.2f}\n📟 <code>{cmd}</code>"
    )
    return jsonify({
        "status": "success",
        "message": "Order placed successfully",
        "order_id": order.id,
        "command": cmd
    })

@app.route('/admin')
@admin_required
def admin_panel():
    users = User.query.all()
    deposits = Deposit.query.order_by(Deposit.id.desc()).limit(50).all()
    orders = Order.query.order_by(Order.id.desc()).limit(50).all()
    services = Service.query.order_by(Service.game, Service.selling_price).all()
    return render_template('admin.html',
                           users=users,
                           deposits=deposits,
                           orders=orders,
                           services=services)

@app.route('/admin/approve_deposit/<int:deposit_id>', methods=['POST'])
@admin_required
@csrf_required
def admin_approve_deposit(deposit_id):
    dep = Deposit.query.get_or_404(deposit_id)
    if dep.status != 'pending':
        flash('Deposit already processed.', 'warning')
        return redirect(url_for('admin_panel'))
    user = User.query.get(dep.user_id)
    if not user:
        abort(404)
    dep.status = 'accepted'
    user.balance += dep.amount
    db.session.commit()
    if dep.telegram_message_id and dep.telegram_chat_id:
        edit_telegram_message(
            dep.telegram_chat_id, dep.telegram_message_id,
            f"✅ <b>ប្រាក់តម្កល់ត្រូវបានយល់ព្រម</b>\n👤 {user.username}\n💵 ${dep.amount:.2f}\n📌 បញ្ចប់"
        )
    flash('Deposit approved. Balance updated.', 'success')
    return redirect(url_for('admin_panel'))

@app.route('/admin/reject_deposit/<int:deposit_id>', methods=['POST'])
@admin_required
@csrf_required
def admin_reject_deposit(deposit_id):
    dep = Deposit.query.get_or_404(deposit_id)
    if dep.status != 'pending':
        flash('Deposit already processed.', 'warning')
        return redirect(url_for('admin_panel'))
    dep.status = 'rejected'
    db.session.commit()
    if dep.telegram_message_id and dep.telegram_chat_id:
        edit_telegram_message(
            dep.telegram_chat_id, dep.telegram_message_id,
            f"❌ <b>ប្រាក់តម្កល់ត្រូវបានបដិសេធ</b>\n👤 {User.query.get(dep.user_id).username}\n💵 ${dep.amount:.2f}"
        )
    flash('Deposit rejected.', 'info')
    return redirect(url_for('admin_panel'))

@app.route('/admin/ban_user/<int:user_id>', methods=['POST'])
@admin_required
@csrf_required
def admin_ban_user(user_id):
    user = User.query.get_or_404(user_id)
    user.is_banned = not user.is_banned
    status = "banned" if user.is_banned else "unbanned"
    db.session.commit()
    flash(f'User {user.username} {status}.', 'success')
    return redirect(url_for('admin_panel'))

@app.route('/admin/add_balance/<int:user_id>', methods=['POST'])
@admin_required
@csrf_required
def admin_add_balance(user_id):
    user = User.query.get_or_404(user_id)
    amount = request.form.get('amount', 0, type=float)
    if amount <= 0:
        flash('Amount must be positive.', 'danger')
    else:
        user.balance += amount
        db.session.commit()
        flash(f'Added ${amount:.2f} to {user.username}. New balance: ${user.balance:.2f}', 'success')
    return redirect(url_for('admin_panel'))

@app.route('/admin/update_price/<int:service_id>', methods=['POST'])
@admin_required
@csrf_required
def admin_update_price(service_id):
    service = Service.query.get_or_404(service_id)
    new_price = request.form.get('selling_price', type=float)
    if new_price is not None and new_price > 0:
        service.selling_price = new_price
        db.session.commit()
        flash('Price updated.', 'success')
    else:
        flash('Invalid price.', 'danger')
    return redirect(url_for('admin_panel'))

@app.route('/webhook/telegram', methods=['POST'])
def telegram_webhook():
    data = request.get_json()
    if not data:
        return 'no data', 400
    if 'callback_query' in data:
        cb = data['callback_query']
        cb_id = cb['id']
        cb_data = cb.get('data', '')
        msg = cb.get('message', {})
        chat_id = msg.get('chat', {}).get('id')
        message_id = msg.get('message_id')
        if cb_data.startswith('accept_') or cb_data.startswith('reject_'):
            action, dep_id = cb_data.split('_')
            dep_id = int(dep_id)
            dep = Deposit.query.get(dep_id)
            if dep and dep.status == 'pending':
                user = User.query.get(dep.user_id)
                if action == 'accept':
                    dep.status = 'accepted'
                    user.balance += dep.amount
                    db.session.commit()
                    edit_telegram_message(
                        chat_id, message_id,
                        f"✅ <b>បានយល់ព្រម</b>\n👤 {user.username}\n💵 ${dep.amount:.2f}\n📌 បញ្ចប់"
                    )
                else:
                    dep.status = 'rejected'
                    db.session.commit()
                    edit_telegram_message(
                        chat_id, message_id,
                        f"❌ <b>បានបដិសេធ</b>\n👤 {user.username}\n💵 ${dep.amount:.2f}"
                    )
            answer_callback(cb_id, "Done")
        return jsonify({"status": "ok"})
    return 'ok', 200

# ------------------------- Database initialization (runs once) -------------------------
with app.app_context():
    db.create_all()
    populate_services()

# ------------------------- Run locally -------------------------
if __name__ == '__main__':
    app.run(debug=True)
