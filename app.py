import os
import random
import string
import json
import tempfile
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

# Load environment variables from .env file (local development)
load_dotenv()

app = Flask(__name__)

# ------------------------- DATABASE CONFIGURATION -------------------------
# Get the DATABASE_URL from environment (if set)
database_url = os.getenv('DATABASE_URL', '')

# If it's a SQLite URL or empty, force it to a writable location in /tmp
if not database_url or 'sqlite:///' in database_url:
    db_path = os.path.join(tempfile.gettempdir(), 'database.db')
    database_url = f'sqlite:///{db_path}'
    print(f"Using writable SQLite database at: {db_path}")

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key-change-me')

# If you ever switch to PostgreSQL, uncomment these:
# if database_url.startswith('postgresql://'):
#     app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
#         'connect_args': {'options': '-c timezone=utc'}
#     }

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'សូមចូលគណនីដើម្បីបន្ត។'

# Telegram configuration (can be missing – code handles it gracefully)
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID')

# ------------------------- DATABASE MODELS -------------------------
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

# ------------------------- LOGIN MANAGER -------------------------
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ------------------------- CSRF PROTECTION -------------------------
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

# ------------------------- ADMIN DECORATOR -------------------------
def admin_required(f):
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated_function

# ------------------------- TELEGRAM HELPERS -------------------------
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

# ------------------------- PRICING & INITIAL DATA -------------------------
def apply_markup(supplier_price):
    if supplier_price < 1.0:
        margin = 0.06
    elif supplier_price < 10.0:
        margin = 0.04
    else:
        margin = 0.03
    return round(supplier_price * (1 + margin), 2)

# -------------------------------------------------------------------
# Prepopulate services from your original data (truncated for readability)
# YOU MUST PASTE YOUR COMPLETE SERVICES_DATA LIST HERE (all 150+ entries)
# -------------------------------------------------------------------
SERVICES_DATA = [
    ("ff", "25", 0.24, "/ff {uid} 25", False),
    ("ff", "100", 0.84, "/ff {uid} 100", False),
    ("ff", "310", 2.59, "/ff {uid} 310", False),
    ("ff", "520", 3.95, "/ff {uid} 520", False),
    ("ff", "1060", 7.95, "/ff {uid} 1060", False),
    ("ff", "2180", 16.15, "/ff {uid} 2180", False),
    ("ff", "5600", 38.75, "/ff {uid} 5600", False),
    ("ff", "11500", 78.55, "/ff {uid} 11500", False),
    ("ff", "Weekly", 1.52, "/ff {uid} Weekly", False),
    # ... add all your other services ...
    ("mc", "4830", 58.27, "/mc {uid} {server_id} 4830", True),
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

# ------------------------- ROUTES (all your original endpoints) -------------------------
# I'm providing a shortened version; you must keep your exact route implementations.
# The following is a template – replace with your full route code.
@app.route('/')
def index():
    services = Service.query.filter_by(is_active=True).all()
    return render_template('index.html', services=services)

@app.route('/register', methods=['GET', 'POST'])
@csrf_required
def register():
    # (your full register code)
    pass

# ... (all other routes: login, dashboard, deposit, admin, api, etc.) ...

# ------------------------- DATABASE INITIALIZATION (RUNS ONCE) -------------------------
with app.app_context():
    db.create_all()
    populate_services()

# ------------------------- LOCAL DEVELOPMENT SERVER -------------------------
if __name__ == '__main__':
    app.run(debug=True)
