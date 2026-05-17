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

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key-change-me')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///database.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'សូមចូលគណនីដើម្បីបន្ត។'

# Telegram config
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID')

# -------------------------------------------------------------------
# Database models
# -------------------------------------------------------------------
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
        # គណនីសកម្ម លុះត្រាតែមិនត្រូវបានហាមឃាត់
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
    status = db.Column(db.String(20), default='pending')      # pending/accepted/rejected
    telegram_message_id = db.Column(db.Integer, nullable=True)
    telegram_chat_id = db.Column(db.String(50), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Order(db.Model):
    __tablename__ = 'orders'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    service_id = db.Column(db.Integer, db.ForeignKey('services.id'), nullable=True)
    game = db.Column(db.String(10), nullable=False)            # ff, mg, pg, hok, mc
    uid = db.Column(db.String(50), nullable=False)
    server_id = db.Column(db.String(50), nullable=True)
    product_name = db.Column(db.String(50), nullable=False)
    price = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), default='pending')       # pending/completed/failed
    command_text = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Service(db.Model):
    __tablename__ = 'services'
    id = db.Column(db.Integer, primary_key=True)
    game = db.Column(db.String(10), nullable=False)
    product_name = db.Column(db.String(50), nullable=False)
    supplier_price = db.Column(db.Float, nullable=False)
    selling_price = db.Column(db.Float, nullable=False)
    command_format = db.Column(db.String(100), nullable=False)     # e.g., '/ff {uid} {product_name}'
    requires_server_id = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)

# -------------------------------------------------------------------
# Login manager
# -------------------------------------------------------------------
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# -------------------------------------------------------------------
# Simple CSRF protection
# -------------------------------------------------------------------
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

# Admin required decorator
def admin_required(f):
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated_function

# -------------------------------------------------------------------
# Telegram helpers
# -------------------------------------------------------------------
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

# -------------------------------------------------------------------
# Auto pricing (very low profit 3-6%)
# -------------------------------------------------------------------
def apply_markup(supplier_price):
    """Mild markup for competitive pricing."""
    if supplier_price < 1.0:
        margin = 0.06  # 6%
    elif supplier_price < 10.0:
        margin = 0.04  # 4%
    else:
        margin = 0.03  # 3%
    return round(supplier_price * (1 + margin), 2)

# Prepopulate services from given data
SERVICES_DATA = [
    # (game, product, supplier_price, command_format, needs_server_id)
    # FREE FIRE
    ("ff", "25", 0.24, "/ff {uid} 25", False),
    ("ff", "100", 0.84, "/ff {uid} 100", False),
    ("ff", "310", 2.59, "/ff {uid} 310", False),
    ("ff", "520", 3.95, "/ff {uid} 520", False),
    ("ff", "1060", 7.95, "/ff {uid} 1060", False),
    ("ff", "2180", 16.15, "/ff {uid} 2180", False),
    ("ff", "5600", 38.75, "/ff {uid} 5600", False),
    ("ff", "11500", 78.55, "/ff {uid} 11500", False),
    ("ff", "Weekly", 1.52, "/ff {uid} Weekly", False),
    ("ff", "Weekly2", 3.04, "/ff {uid} Weekly2", False),
    ("ff", "Weekly3", 4.56, "/ff {uid} Weekly3", False),
    ("ff", "Weekly4", 6.08, "/ff {uid} Weekly4", False),
    ("ff", "Weekly5", 7.60, "/ff {uid} Weekly5", False),
    ("ff", "WeeklyLite", 0.32, "/ff {uid} WeeklyLite", False),
    ("ff", "WeeklyLite2", 0.64, "/ff {uid} WeeklyLite2", False),
    ("ff", "WeeklyLite3", 0.96, "/ff {uid} WeeklyLite3", False),
    ("ff", "WeeklyLite4", 1.28, "/ff {uid} WeeklyLite4", False),
    ("ff", "WeeklyLite5", 1.60, "/ff {uid} WeeklyLite5", False),
    ("ff", "Monthly", 7.30, "/ff {uid} Monthly", False),
    ("ff", "Monthly2", 14.60, "/ff {uid} Monthly2", False),
    ("ff", "Monthly3", 21.90, "/ff {uid} Monthly3", False),
    ("ff", "Monthly4", 29.20, "/ff {uid} Monthly4", False),
    ("ff", "Monthly5", 36.50, "/ff {uid} Monthly5", False),
    ("ff", "Evo3D", 0.70, "/ff {uid} Evo3D", False),
    ("ff", "Evo7D", 0.85, "/ff {uid} Evo7D", False),
    ("ff", "Evo30D", 2.50, "/ff {uid} Evo30D", False),
    ("ff", "Level6", 0.32, "/ff {uid} Level6", False),
    ("ff", "Level10", 0.68, "/ff {uid} Level10", False),
    ("ff", "Level15", 0.68, "/ff {uid} Level15", False),
    ("ff", "Level20", 0.68, "/ff {uid} Level20", False),
    ("ff", "Level25", 0.68, "/ff {uid} Level25", False),
    ("ff", "Level30", 0.68, "/ff {uid} Level30", False),
    # MOBILE LEGENDS (requires server id)
    ("mg", "55", 0.84, "/mg {uid} {server_id} 55", True),
    ("mg", "86", 1.25, "/mg {uid} {server_id} 86", True),
    ("mg", "112", 1.76, "/mg {uid} {server_id} 112", True),
    ("mg", "165", 2.45, "/mg {uid} {server_id} 165", True),
    ("mg", "172", 2.55, "/mg {uid} {server_id} 172", True),
    ("mg", "257", 3.66, "/mg {uid} {server_id} 257", True),
    ("mg", "275", 3.89, "/mg {uid} {server_id} 275", True),
    ("mg", "343", 4.90, "/mg {uid} {server_id} 343", True),
    ("mg", "429", 6.35, "/mg {uid} {server_id} 429", True),
    ("mg", "514", 7.35, "/mg {uid} {server_id} 514", True),
    ("mg", "565", 7.89, "/mg {uid} {server_id} 565", True),
    ("mg", "600", 8.55, "/mg {uid} {server_id} 600", True),
    ("mg", "706", 9.89, "/mg {uid} {server_id} 706", True),
    ("mg", "792", 11.09, "/mg {uid} {server_id} 792", True),
    ("mg", "878", 12.35, "/mg {uid} {server_id} 878", True),
    ("mg", "963", 13.49, "/mg {uid} {server_id} 963", True),
    ("mg", "1049", 14.95, "/mg {uid} {server_id} 1049", True),
    ("mg", "1135", 15.69, "/mg {uid} {server_id} 1135", True),
    ("mg", "1220", 16.99, "/mg {uid} {server_id} 1220", True),
    ("mg", "1412", 19.95, "/mg {uid} {server_id} 1412", True),
    ("mg", "1584", 22.49, "/mg {uid} {server_id} 1584", True),
    ("mg", "1755", 24.99, "/mg {uid} {server_id} 1755", True),
    ("mg", "1926", 26.99, "/mg {uid} {server_id} 1926", True),
    ("mg", "2195", 29.95, "/mg {uid} {server_id} 2195", True),
    ("mg", "2538", 34.99, "/mg {uid} {server_id} 2538", True),
    ("mg", "2901", 39.49, "/mg {uid} {server_id} 2901", True),
    ("mg", "3688", 49.99, "/mg {uid} {server_id} 3688", True),
    ("mg", "4394", 59.55, "/mg {uid} {server_id} 4394", True),
    ("mg", "5532", 74.49, "/mg {uid} {server_id} 5532", True),
    ("mg", "6238", 85.49, "/mg {uid} {server_id} 6238", True),
    ("mg", "6944", 97.52, "/mg {uid} {server_id} 6944", True),
    ("mg", "7727", 101.99, "/mg {uid} {server_id} 7727", True),
    ("mg", "8433", 121.25, "/mg {uid} {server_id} 8433", True),
    ("mg", "9288", 127.08, "/mg {uid} {server_id} 9288", True),
    ("mg", "10700", 147.25, "/mg {uid} {server_id} 10700", True),
    ("mg", "Weekly", 1.55, "/mg {uid} {server_id} Weekly", True),
    ("mg", "Weekly2", 3.10, "/mg {uid} {server_id} Weekly2", True),
    ("mg", "Weekly3", 4.65, "/mg {uid} {server_id} Weekly3", True),
    ("mg", "Weekly4", 6.20, "/mg {uid} {server_id} Weekly4", True),
    ("mg", "Weekly5", 7.75, "/mg {uid} {server_id} Weekly5", True),
    ("mg", "172+wkp", 4.25, "/mg {uid} {server_id} 172+wkp", True),
    ("mg", "257+wkp", 5.25, "/mg {uid} {server_id} 257+wkp", True),
    ("mg", "Twilight", 8.70, "/mg {uid} {server_id} Twilight", True),
    ("mg", "50x2", 0.94, "/mg {uid} {server_id} 50x2", True),
    ("mg", "150x2", 2.55, "/mg {uid} {server_id} 150x2", True),
    ("mg", "250x2", 3.90, "/mg {uid} {server_id} 250x2", True),
    ("mg", "500x2", 8.25, "/mg {uid} {server_id} 500x2", True),
    ("mg", "ValuePass", 0.86, "/mg {uid} {server_id} ValuePass", True),
    ("mg", "WEB", 0.95, "/mg {uid} {server_id} WEB", True),
    ("mg", "MEB", 4.10, "/mg {uid} {server_id} MEB", True),
    # PUBG MOBILE
    ("pg", "60", 0.99, "/pg {uid} 60", False),
    ("pg", "325", 4.50, "/pg {uid} 325", False),
    ("pg", "660", 9.25, "/pg {uid} 660", False),
    ("pg", "1800", 22.29, "/pg {uid} 1800", False),
    ("pg", "3850", 44.49, "/pg {uid} 3850", False),
    ("pg", "8100", 88.29, "/pg {uid} 8100", False),
    # HONOR OF KINGS
    ("hok", "16", 0.22, "/hok {uid} 16", False),
    ("hok", "80", 0.95, "/hok {uid} 80", False),
    ("hok", "240", 2.68, "/hok {uid} 240", False),
    ("hok", "400", 4.49, "/hok {uid} 400", False),
    ("hok", "560", 6.25, "/hok {uid} 560", False),
    ("hok", "830", 8.95, "/hok {uid} 830", False),
    ("hok", "1245", 12.99, "/hok {uid} 1245", False),
    ("hok", "2508", 25.99, "/hok {uid} 2508", False),
    ("hok", "4180", 43.25, "/hok {uid} 4180", False),
    ("hok", "8360", 86.50, "/hok {uid} 8360", False),
    # MAGIC CHESS GOGO (requires server id)
    ("mc", "Weekly", 1.90, "/mc {uid} {server_id} Weekly", True),
    ("mc", "5", 0.15, "/mc {uid} {server_id} 5", True),
    ("mc", "12", 0.30, "/mc {uid} {server_id} 12", True),
    ("mc", "19", 0.49, "/mc {uid} {server_id} 19", True),
    ("mc", "28", 0.69, "/mc {uid} {server_id} 28", True),
    ("mc", "44", 0.79, "/mc {uid} {server_id} 44", True),
    ("mc", "59", 0.99, "/mc {uid} {server_id} 59", True),
    ("mc", "85", 1.19, "/mc {uid} {server_id} 85", True),
    ("mc", "170", 2.35, "/mc {uid} {server_id} 170", True),
    ("mc", "240", 3.35, "/mc {uid} {server_id} 240", True),
    ("mc", "296", 3.99, "/mc {uid} {server_id} 296", True),
    ("mc", "408", 5.89, "/mc {uid} {server_id} 408", True),
    ("mc", "568", 7.70, "/mc {uid} {server_id} 568", True),
    ("mc", "875", 11.85, "/mc {uid} {server_id} 875", True),
    ("mc", "2010", 25.25, "/mc {uid} {server_id} 2010", True),
    ("mc", "4830", 58.27, "/mc {uid} {server_id} 4830", True),
]

def populate_services():
    """Insert services into DB if empty."""
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

# -------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------
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
        # Telegram notification
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

        # Send Telegram message with inline buttons
        inline_keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ ទទួល (Accept)", "callback_data": f"accept_{dep.id}"},
                    {"text": "❌ បដិសេធ (Reject)", "callback_data": f"reject_{dep.id}"}
                ]
            ]
        }
        msg = send_telegram(
            ADMIN_CHAT_ID,
            f"💰 <b>ប្រាក់តម្កល់ថ្មី</b>\n"
            f"👤 អ្នកប្រើ៖ {current_user.username}\n"
            f"💵 ចំនួន៖ ${amount:.2f}\n"
            f"📌 ស្ថានភាព៖ កំពុងរងចាំ",
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

# ======================== API endpoint ========================
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

    # Game names mapping
    if game in ['mg', 'mc'] and not server_id:
        return jsonify({"status": "error", "message": "Server ID required for this game"}), 400

    # Find service
    service = Service.query.filter_by(game=game, product_name=product, is_active=True).first()
    if not service:
        return jsonify({"status": "error", "message": "Service not found"}), 404

    price = service.selling_price
    if user.balance < price:
        return jsonify({"status": "error", "message": "Insufficient balance"}), 402

    # Build command
    if service.requires_server_id:
        cmd = service.command_format.format(uid=uid, server_id=server_id, product_name=product)
    else:
        cmd = service.command_format.format(uid=uid, product_name=product)

    # Deduct balance
    user.balance -= price
    order = Order(
        user_id=user.id, service_id=service.id,
        game=game, uid=uid, server_id=server_id,
        product_name=product, price=price,
        status='pending', command_text=cmd
    )
    db.session.add(order)
    db.session.commit()

    # Notify admin
    send_telegram(
        ADMIN_CHAT_ID,
        f"🛒 <b>បញ្ជាទិញថ្មី</b>\n"
        f"👤 {user.username}\n"
        f"🎮 {game.upper()} | {product}\n"
        f"🆔 {uid}" + (f" | 🌐 {server_id}" if server_id else "") + "\n"
        f"💰 ${price:.2f}\n"
        f"📟 <code>{cmd}</code>"
    )

    return jsonify({
        "status": "success",
        "message": "Order placed successfully",
        "order_id": order.id,
        "command": cmd
    })

# ======================== Admin panel ========================
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
    # Update Telegram message if exists
    if dep.telegram_message_id and dep.telegram_chat_id:
        edit_telegram_message(
            dep.telegram_chat_id, dep.telegram_message_id,
            f"✅ <b>ប្រាក់តម្កល់ត្រូវបានយល់ព្រម</b>\n"
            f"👤 {user.username}\n"
            f"💵 ${dep.amount:.2f}\n"
            f"📌 បញ្ចប់"
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
            f"❌ <b>ប្រាក់តម្កល់ត្រូវបានបដិសេធ</b>\n"
            f"👤 {User.query.get(dep.user_id).username}\n"
            f"💵 ${dep.amount:.2f}"
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

# ======================== Telegram webhook ========================
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
                        f"✅ <b>បានយល់ព្រម</b>\n"
                        f"👤 {user.username}\n"
                        f"💵 ${dep.amount:.2f}\n"
                        f"📌 បញ្ចប់"
                    )
                else:  # reject
                    dep.status = 'rejected'
                    db.session.commit()
                    edit_telegram_message(
                        chat_id, message_id,
                        f"❌ <b>បានបដិសេធ</b>\n"
                        f"👤 {user.username}\n"
                        f"💵 ${dep.amount:.2f}"
                    )
            answer_callback(cb_id, "Done")
        return jsonify({"status": "ok"})
    return 'ok', 200

def answer_callback(callback_id, text=""):
    if not BOT_TOKEN:
        return
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery'
    payload = {'callback_query_id': callback_id, 'text': text}
    requests.post(url, json=payload, timeout=10)

# -------------------------------------------------------------------
# Run
# -------------------------------------------------------------------
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        populate_services()
    app.run(debug=True)