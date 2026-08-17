from flask import Flask, render_template, request, flash, redirect, url_for, session, jsonify, make_response, g
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import case, update
from sqlalchemy.orm import joinedload, selectinload
from sqlalchemy.schema import CreateIndex
from datetime import datetime, timedelta
from functools import wraps
import csv
import hmac
import io
import os
from pathlib import Path
import secrets
from werkzeug.security import generate_password_hash, check_password_hash


PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_ENV = os.environ.get('APP_ENV', 'development').strip().lower()
DEFAULT_SECRET_KEY = 'dev-only-change-me'
PLACEHOLDER_SECRET_KEY = 'replace-with-openssl-rand-hex-32'
secret_key = os.environ.get('SECRET_KEY', DEFAULT_SECRET_KEY)
if APP_ENV == 'production' and (
    secret_key in {DEFAULT_SECRET_KEY, PLACEHOLDER_SECRET_KEY}
    or len(secret_key) < 32
):
    raise RuntimeError('生产环境必须设置至少 32 个字符的 SECRET_KEY。')

app = Flask(
    __name__,
    instance_path=str(PROJECT_ROOT / 'instance'),
    instance_relative_config=True,
)

# 生产环境请通过环境变量 SECRET_KEY 注入随机强密钥
app.config['SECRET_KEY'] = secret_key
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///storage.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get(
    'SESSION_COOKIE_SECURE',
    '1' if APP_ENV == 'production' else '0',
) == '1'
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # CSV 导入最大 5MB
seed_demo_data = os.environ.get(
    'SEED_DEMO_DATA',
    '0' if APP_ENV == 'production' else '1',
) == '1'
if APP_ENV == 'production' and seed_demo_data:
    raise RuntimeError('生产环境禁止启用 SEED_DEMO_DATA。')
app.config['SEED_DEMO_DATA'] = seed_demo_data
db = SQLAlchemy(app)

# ==================== 🛠️ 原生无损兼容的 SQLite 数据库模型 ====================

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(50), nullable=False)
    role = db.Column(db.String(20), default='user')          
    credit_score = db.Column(db.Integer, default=100)       
    quota_limit = db.Column(db.Integer, default=20)         
    # 100% 保留原有关系，防止其他界面崩盘
    borrows = db.relationship('BorrowRecord', backref='borrower', lazy=True)

class MainCategory(db.Model):
    """一级大类（如：开发板、办公电子）"""
    __tablename__ = 'main_categories'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    sub_categories = db.relationship('SubCategory', backref='main', lazy=True, cascade="all, delete-orphan")

class SubCategory(db.Model):
    """二级小类（如：STM32、ESP32、显示器）"""
    __tablename__ = 'sub_categories'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    main_id = db.Column(db.Integer, db.ForeignKey('main_categories.id'), nullable=False)


class Item(db.Model):
    __tablename__ = 'items'
    id = db.Column(db.Integer, primary_key=True)
    asset_id = db.Column(db.String(50), unique=True, nullable=False) # 资产编号
    name = db.Column(db.String(100), nullable=False)                 # 物资名称
    category = db.Column(db.String(50), nullable=False)              # 一级大类
    sub_category = db.Column(db.String(50), default='通用')           # 二级小类
    status = db.Column(db.String(20), default='运行中')               # 状态（运行中/急需维修）
    status_color = db.Column(db.String(20), default='emerald')       # 状态颜色
    location = db.Column(db.String(100), nullable=False)             # 存放位置
    stock = db.Column(db.Integer, default=10)                        # 当前在库库存数量
    min_stock = db.Column(db.Integer, default=3)                     # 最低安全预警红线
    
    # 100% 保留原有反向引用，成功化解 NoForeignKeysError！
    borrows = db.relationship('BorrowRecord', backref='item', lazy=True)


class BorrowRecord(db.Model):
    """✨ 既兼容单件、又支持暂存箱合并申领的超级兼容模型表"""
    __tablename__ = 'borrow_records'
    __table_args__ = (
        db.Index('ix_borrow_records_user_status', 'user_id', 'status'),
        db.Index('ix_borrow_records_user_borrow_date', 'user_id', 'borrow_date'),
        db.Index('ix_borrow_records_status_borrow_date', 'status', 'borrow_date'),
        db.Index('ix_borrow_records_borrow_date', 'borrow_date'),
        db.Index('ix_borrow_records_item_id', 'item_id'),
    )
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    
    # 🌟 关键外键留存：重组成功，旧页面原生关联字段完美兼容契合
    item_id = db.Column(db.Integer, db.ForeignKey('items.id'), nullable=False) 
    
    borrow_date = db.Column(db.DateTime, default=datetime.now)  # 规范为本地时间
    return_date = db.Column(db.DateTime, nullable=True)        # 兼容老数据，设为可空
    actual_return_date = db.Column(db.DateTime, nullable=True) 
    reason = db.Column(db.Text, default='单件快捷申领')          # 设定默认用途事由
    status = db.Column(db.String(20), default='等待审批')         # 等待审批/进行中/待归还审核/已归还/已拒绝
    
    # 级联绑定明细子表
    details = db.relationship('BorrowDetail', backref='record', lazy=True, cascade="all, delete-orphan")


class BorrowDetail(db.Model):
    """专门用来存放暂存箱批量申领拆分出的明细表"""
    __tablename__ = 'borrow_details'
    __table_args__ = (
        db.Index('ix_borrow_details_record_id', 'record_id'),
        db.Index('ix_borrow_details_item_id', 'item_id'),
    )
    id = db.Column(db.Integer, primary_key=True)
    record_id = db.Column(db.Integer, db.ForeignKey('borrow_records.id'), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey('items.id'), nullable=False)
    quantity = db.Column(db.Integer, default=1)                          # 借用数量
    
    item = db.relationship('Item', backref='borrow_details', lazy=True)


# ==================== 🐍 路由与身份分流控制 ====================

def current_user():
    if 'current_user' not in g:
        user_id = session.get('user_id')
        g.current_user = db.session.get(User, user_id) if user_id else None
    return g.current_user


def is_api_request():
    return request.path.startswith('/api/') or request.is_json


CSRF_SESSION_KEY = '_csrf_token'
SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}


def csrf_token():
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


app.jinja_env.globals['csrf_token'] = csrf_token


@app.before_request
def validate_csrf_token():
    if request.method in SAFE_METHODS:
        return None

    # 未登录 API 仍由认证装饰器返回 401；登录和注册本身始终校验 CSRF。
    if request.endpoint not in {'login', 'register'} and not session.get('user_id'):
        return None

    expected = session.get(CSRF_SESSION_KEY)
    submitted = request.headers.get('X-CSRF-Token') or request.form.get(CSRF_SESSION_KEY)
    if expected and submitted and hmac.compare_digest(expected, submitted):
        return None

    message = '请求校验失败，请刷新页面后重试。'
    if is_api_request():
        return jsonify({'success': False, 'msg': message}), 400
    return message, 400


@app.after_request
def add_security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
    if APP_ENV == 'production':
        response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000')
    if session.get('user_id'):
        response.headers.setdefault('Cache-Control', 'no-store')
    return response


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            session.clear()
            if is_api_request():
                return jsonify({'success': False, 'msg': '登录会话失效，请重新登录系统！'}), 401
            flash('请先登录后再访问系统。', 'error')
            return redirect(url_for('login'))
        session['role'] = user.role
        session.setdefault('view_mode', user.role)
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user or user.role != 'admin' or session.get('view_mode') == 'user':
            if is_api_request():
                return jsonify({'success': False, 'msg': '您无权执行该管理员操作！'}), 403
            flash('安全拦截：您无权访问管理员后台。', 'error')
            return redirect(url_for('user_dashboard') if user else url_for('login'))
        return view(*args, **kwargs)
    return wrapped


def parse_non_negative_int(value, default=0):
    try:
        number = int(value)
        return number if number >= 0 else default
    except (TypeError, ValueError):
        return default


def parse_positive_int(value):
    try:
        number = int(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def valid_item_text_lengths(name, asset_id, category, sub_category, location, status):
    return (
        len(name) <= 100
        and len(asset_id) <= 50
        and len(category) <= 50
        and len(sub_category) <= 50
        and len(location) <= 100
        and len(status) <= 20
    )


def normalize_item_state(item):
    if item.stock <= 0:
        item.stock = 0
        item.status = '无库存'
        item.status_color = 'error'
    elif '维修' in (item.status or ''):
        item.status = '急需维修'
        item.status_color = 'error'
    else:
        item.status = '库存充足'
        item.status_color = 'emerald'


def user_borrow_quantity(user_id, statuses):
    quantity = db.session.query(
        db.func.coalesce(db.func.sum(case(
            (BorrowDetail.id.is_(None), 1),
            (BorrowDetail.quantity > 0, BorrowDetail.quantity),
            else_=0,
        )), 0)
    ).select_from(BorrowRecord).outerjoin(
        BorrowDetail,
        BorrowDetail.record_id == BorrowRecord.id,
    ).filter(
        BorrowRecord.user_id == user_id,
        BorrowRecord.status.in_(statuses),
    ).scalar()
    return int(quantity or 0)


def user_reserved_quantity(user_id):
    active_statuses = ['等待审批', '进行中', '待归还审核']
    return user_borrow_quantity(user_id, active_statuses)


def lock_user_row(user_id):
    result = db.session.execute(
        update(User)
        .where(User.id == user_id)
        .values(quota_limit=User.quota_limit)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def borrow_records_with_related(query):
    return query.options(
        joinedload(BorrowRecord.borrower),
        joinedload(BorrowRecord.item),
        selectinload(BorrowRecord.details).joinedload(BorrowDetail.item),
    )


def build_category_tree():
    tree = {}
    for main in MainCategory.query.order_by(MainCategory.name.asc()).all():
        tree[main.name] = {
            'id': main.id,
            'subs': [
                {'id': sub.id, 'name': sub.name}
                for sub in sorted(main.sub_categories, key=lambda sub: sub.name)
            ]
        }
    return tree


def decode_csv_upload(file_storage):
    raw = file_storage.stream.read()
    for encoding in ('utf-8-sig', 'utf-8', 'gb18030'):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError('无法识别 CSV 编码，请使用 UTF-8 或 GBK/GB18030 编码保存后重试')


def rollback_and_log(operation):
    db.session.rollback()
    app.logger.exception('%s failed', operation)


def csv_safe_cell(value):
    if not isinstance(value, str):
        return value
    if value.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + value
    return value


@app.route('/healthz')
def healthz():
    return jsonify({'status': 'ok', 'time': datetime.now().isoformat(timespec='seconds')})

# ==================== 🐍 身份验证流（登录与安全注册） ====================

@app.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        user = None
        if email and password and len(email) <= 50 and len(password) <= 128:
            user = User.query.filter_by(username=email).first()

        password_ok = bool(user and check_password_hash(user.password, password))

        if user and password_ok:
            session.clear()
            session['user_id'] = user.id
            session['role'] = user.role
            session['view_mode'] = user.role 
            
            flash(f'登录成功！欢迎回来，{user.full_name}。', 'success')
            return redirect(url_for('admin_dashboard' if user.role == 'admin' else 'user_dashboard'))
        else:
            flash('账号或密码错误，请重试', 'error')
            return render_template('login.html', email=email)
            
    return render_template('login.html')


@app.route('/api/register', methods=['POST'])
def register():
    """
    用户自主注册接口（密码强哈希加密）
    """
    username = request.form.get('reg_username', '').strip()
    password = request.form.get('reg_password')
    full_name = request.form.get('reg_full_name', '').strip()

    if not username or not password or not full_name:
        flash('注册失败：请完整填写所有必填信息！', 'error')
        return redirect(url_for('login'))
    if len(username) > 50 or len(full_name) > 50:
        flash('注册失败：账户名和姓名均不能超过 50 个字符。', 'error')
        return redirect(url_for('login'))
    if not 12 <= len(password) <= 128:
        flash('注册失败：密码长度须为 12–128 位。', 'error')
        return redirect(url_for('login'))

    # 1. 查重机制：防止账户名发生碰撞
    if User.query.filter_by(username=username).first():
        flash(f'注册失败：用户名【{username}】已被占用！', 'error')
        return redirect(url_for('login'))

    try:
        # 2. 🌟 核心增益：使用 pbkdf2:sha256 算法对明文密码进行单向哈希加盐加密
        hashed_password = generate_password_hash(password, method='pbkdf2:sha256', salt_length=16)

        # 3. 构建新用户（自主注册用户默认权限为 'user'）
        new_user = User(
            username=username,
            password=hashed_password, # 存储不可逆的密文
            full_name=full_name,
            role='user',
            credit_score=100,         # 新用户初始满分授信
            quota_limit=20            # 初始申领额度
        )
        db.session.add(new_user)
        db.session.commit()
        
        flash('🎉 恭喜您，账户注册成功！请使用刚注册的账号进行登录。', 'success')
    except Exception:
        rollback_and_log('register user')
        flash('系统错误，注册失败，请稍后重试。', 'error')

    return redirect(url_for('login'))


@app.route('/switch-mode', methods=['POST'])
@login_required
def switch_mode():
    user = current_user()
    if user.role != 'admin':
        flash('您不是管理员，无法切换视图模式！', 'error')
        return redirect(url_for('user_dashboard'))
    
    if session.get('view_mode') == 'admin':
        session['view_mode'] = 'user'
        flash('已成功进入【普通用户视图】。您可以开始申领或查看个人物资。', 'success')
        return redirect(url_for('user_dashboard'))
    else:
        session['view_mode'] = 'admin'
        flash('已成功返回【超级管理员控制台】。', 'success')
        return redirect(url_for('admin_dashboard'))


@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    total_items = db.session.query(db.func.sum(Item.stock)).scalar() or 0
    user_count = User.query.count()
    active_borrows = BorrowRecord.query.filter_by(status='进行中').count()
    pending_approvals = BorrowRecord.query.filter(BorrowRecord.status.in_(['等待审批', '待归还审核'])).count()
    error_items = Item.query.filter_by(status='急需维修').count()

    recent_borrows = borrow_records_with_related(
        BorrowRecord.query.order_by(BorrowRecord.borrow_date.desc()).limit(5)
    ).all()
    users_list = User.query.filter_by(role='user').order_by(User.credit_score.asc()).limit(4).all()
    admin_info = current_user()
    
    return render_template(
        'admin_dashboard.html',
        active_page='dashboard',
        user_info=admin_info,
        view_mode=session.get('view_mode'), 
        total_items=total_items,
        user_count=user_count,
        active_borrows=active_borrows,
        pending_approvals=pending_approvals,
        error_items=error_items,
        recent_borrows=recent_borrows,
        users_list=users_list
    )


@app.route('/user/dashboard')
@login_required
def user_dashboard():
    user_info = current_user()
    current_user_id = user_info.id
    my_active_count = user_borrow_quantity(current_user_id, ['进行中'])
    my_pending_count = BorrowRecord.query.filter_by(user_id=current_user_id, status='等待审批').count()
    quota_used = user_reserved_quantity(current_user_id)
    my_borrows = borrow_records_with_related(
        BorrowRecord.query.filter_by(user_id=current_user_id)
        .order_by(BorrowRecord.borrow_date.desc())
        .limit(5)
    ).all()

    return render_template(
        'user_dashboard.html',
        active_page='dashboard',
        user_info=user_info,
        view_mode=session.get('view_mode'), 
        my_active_count=my_active_count,
        my_pending_count=my_pending_count,
        quota_used=quota_used,
        my_borrows=my_borrows
    )

@app.route('/catalog')
@login_required
def catalog():
    user_info = current_user()
    
    items_from_db = Item.query.all()
    main_categories = MainCategory.query.all()
    
    db_sub_categories = {}
    for main in main_categories:
        db_sub_categories[main.name] = [sub.name for sub in main.sub_categories]
    
    db_sub_categories['all'] = []
    for main in main_categories:
        for sub in main.sub_categories:
            if sub.name not in db_sub_categories['all']:
                db_sub_categories['all'].append(sub.name)

    return render_template(
        'catalog.html', 
        active_page='catalog', 
        user_info=user_info,
        view_mode=session.get('view_mode'),
        main_categories=main_categories,      
        sub_category_data=db_sub_categories,  
        items_list=items_from_db
    )


# ✨✨ 核心对接：处理前端批量加购合并提交的 API 异步接收中心 ✨✨
@app.route('/api/borrow/submit', methods=['POST'])
@login_required
def submit_batch_borrow():
    user = current_user()
        
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'success': False, 'msg': '未接收到有效的数据传输包！'}), 400

    reason = data.get('reason', '').strip()
    return_date_str = data.get('return_date')
    cart_items = data.get('items', [])

    if not reason or not return_date_str or not isinstance(cart_items, list) or not cart_items:
        return jsonify({'success': False, 'msg': '合并失败：请补全用途事由或预计归还日期！'}), 400
    if len(reason) > 500:
        return jsonify({'success': False, 'msg': '借用用途请控制在 500 字以内。'}), 400
    if len(cart_items) > 50:
        return jsonify({'success': False, 'msg': '单次合并申请最多支持 50 类物资。'}), 400

    try:
        return_date = datetime.strptime(return_date_str, '%Y-%m-%d')
        if return_date.date() < datetime.now().date():
            return jsonify({'success': False, 'msg': '预计归还日期不能早于今天！'}), 400

        requested_quantities = {}
        for chunk in cart_items:
            if not isinstance(chunk, dict):
                return jsonify({'success': False, 'msg': '申请明细格式不正确。'}), 400
            item_id = parse_positive_int(chunk.get('item_id'))
            req_qty = parse_positive_int(chunk.get('quantity', 1))
            if not item_id or not req_qty:
                return jsonify({'success': False, 'msg': '物资 ID 或申请数量不合法。'}), 400
            requested_quantities[item_id] = requested_quantities.get(item_id, 0) + req_qty

        requested_item_ids = list(requested_quantities.keys())
        items = Item.query.filter(Item.id.in_(requested_item_ids)).all()
        items_by_id = {item.id: item for item in items}
        missing_ids = [str(item_id) for item_id in requested_quantities if item_id not in items_by_id]
        if missing_ids:
            return jsonify({'success': False, 'msg': f'以下物资不存在或已下架：{", ".join(missing_ids)}'}), 400

        for item_id, req_qty in requested_quantities.items():
            target_item = items_by_id[item_id]
            if '维修' in (target_item.status or ''):
                return jsonify({'success': False, 'msg': f'【{target_item.name}】处于维修状态，暂不可申领。'}), 400
            if target_item.stock < req_qty:
                return jsonify({
                    'success': False,
                    'msg': f'【{target_item.name}】库存水位告急，剩余可借 {target_item.stock} 件'
                }), 400

        # 对用户行做一次无值变化的条件更新，在 SQLite/PostgreSQL 上序列化同一用户的额度校验。
        if not lock_user_row(user.id):
            db.session.rollback()
            session.clear()
            return jsonify({'success': False, 'msg': '用户状态已失效，请重新登录。'}), 401

        total_requested = sum(requested_quantities.values())
        reserved_quantity = user_reserved_quantity(user.id)
        if reserved_quantity + total_requested > user.quota_limit:
            remaining_quota = max(user.quota_limit - reserved_quantity, 0)
            db.session.rollback()
            return jsonify({
                'success': False,
                'msg': f'申请数量超过当前额度，剩余可申请 {remaining_quota} 件。'
            }), 400

        # 抓取第一件物资的 ID 用作老模型 item_id 的强制无损兼容垫底
        fallback_item_id = next(iter(requested_quantities))

        # 录入借用单主表记录
        new_record = BorrowRecord(
            user_id=user.id,
            item_id=fallback_item_id, 
            reason=reason, 
            return_date=return_date, 
            status='等待审批'
        )
        db.session.add(new_record)
        db.session.flush() 

        # 循环灌入明细子表
        for item_id, req_qty in requested_quantities.items():
            db.session.add(BorrowDetail(
                record_id=new_record.id, 
                item_id=item_id, 
                quantity=req_qty
            ))

        db.session.commit()
        return jsonify({'success': True, 'msg': '您的合并批量申请单已成功挂载至决策中心，请等待审批！'})
        
    except Exception:
        rollback_and_log('submit batch borrow')
        return jsonify({'success': False, 'msg': '系统处理申请失败，请稍后重试。'}), 500

@app.route('/borrow', methods=['GET'])
@login_required
def borrow():
    """
    我的借用：用户中央控制流水看板视图（加入防 None 崩溃托底机制）
    """
    current_user_id = session.get('user_id')
    if not current_user_id:
        flash('会话过期或未登录，请先登录系统！', 'error')
        return redirect(url_for('login'))

    # 1. 动态抓取当前登录用户上下文
    user_info = db.session.get(User, current_user_id)

    # 🚨 核心修复防护线：如果由于数据库重置导致 session 里的 user_id 找不到对应的用户实体
    if not user_info:
        session.clear() # 清理掉过期的脏 session
        flash('您的登录凭证在新数据库中已失效，请重新登录！', 'warning')
        return redirect(url_for('login'))

    # 2. 正常拉取单据明细
    my_borrows = borrow_records_with_related(
        BorrowRecord.query.filter_by(user_id=current_user_id)
        .order_by(BorrowRecord.borrow_date.desc())
    ).all()

    # 3. 计算微观指标看板
    my_active_count = BorrowRecord.query.filter_by(user_id=current_user_id, status='进行中').count()
    my_pending_count = BorrowRecord.query.filter_by(user_id=current_user_id, status='等待审批').count()

    return render_template(
        'borrow.html',
        active_page='borrow',               
        user_info=user_info,                
        view_mode=session.get('view_mode'), 
        my_active_count=my_active_count,
        my_pending_count=my_pending_count,
        my_borrows=my_borrows               
    )

@app.route('/api/borrow/revoke/<int:id>', methods=['POST'])
@login_required
def revoke_borrow(id):
    """
    异步撤回接口：允许用户在管理员审批前自主拦截申请
    """
    current_user_id = session.get('user_id')
    if not current_user_id:
        return jsonify({'success': False, 'msg': '会话已过期，请重新登录！'}), 401

    record = db.get_or_404(BorrowRecord, id)
    if record.user_id != current_user_id:
        return jsonify({'success': False, 'msg': '安全拦截：您无权操作他人的资产单据！'}), 403

    try:
        claim = db.session.execute(
            update(BorrowRecord)
            .where(
                BorrowRecord.id == id,
                BorrowRecord.user_id == current_user_id,
                BorrowRecord.status == '等待审批',
            )
            .values(status='撤回处理中')
            .execution_options(synchronize_session=False)
        )
        if claim.rowcount != 1:
            db.session.rollback()
            return jsonify({'success': False, 'msg': '撤回失败：该单据已在处理中，无法取消！'}), 409
        db.session.delete(record)
        db.session.commit()
        return jsonify({'success': True, 'msg': '🎉 申请单据已成功撤回，暂存额度已即时返还！'})
    except Exception:
        rollback_and_log('revoke borrow')
        return jsonify({'success': False, 'msg': '撤回失败，请稍后重试。'}), 500


@app.route('/api/borrow/return/<int:id>', methods=['POST'])
@login_required
def return_borrow(id):
    """
    用户自主发起设备还库申请
    """
    current_user_id = session.get('user_id')
    if not current_user_id:
        return jsonify({'success': False, 'msg': '会话已过期，请重新登录！'}), 401

    record = db.get_or_404(BorrowRecord, id)
    if record.user_id != current_user_id:
        return jsonify({'success': False, 'msg': '越权拦截：您无法归还不属于您的物资！'}), 403

    try:
        transition = db.session.execute(
            update(BorrowRecord)
            .where(
                BorrowRecord.id == id,
                BorrowRecord.user_id == current_user_id,
                BorrowRecord.status == '进行中',
            )
            .values(status='待归还审核')
            .execution_options(synchronize_session=False)
        )
        if transition.rowcount != 1:
            db.session.rollback()
            return jsonify({'success': False, 'msg': '该单据状态已变化，无法执行归还操作！'}), 409
        db.session.commit()
        return jsonify({'success': True, 'msg': '🎉 归还申请已提交！请将物资放回原处，等待管理员审核入库。'})
    except Exception:
        rollback_and_log('request borrow return')
        return jsonify({'success': False, 'msg': '归还申请失败，请稍后重试。'}), 500


@app.route('/setting')
@login_required
def setting(): return "<h3>系统设置页面正在全力开发中...</h3><br><a href='javascript:history.back()'>返回上一页</a>"

@app.route('/logout', methods=['POST'])
@login_required
def logout():
    session.clear()  
    flash('您已成功安全退出登录', 'success')
    return redirect(url_for('login'))

@app.route('/person')
@login_required
def person(): return "<h3>个人中心页面（开发中...）</h3><br><a href='javascript:history.back()'>返回上一页</a>"


# ==================== 📦 动态自适应智能入库大盘总线 ====================
@app.route('/inventory', methods=['GET', 'POST'])
@admin_required
def inventory():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        asset_id = request.form.get('asset_id', '').strip()
        location = request.form.get('location', '').strip() or "未知库区"
        stock = parse_non_negative_int(request.form.get('stock'), 0)
        status = request.form.get('status', '').strip() or "运行中"

        if not name or not asset_id:
            flash('入库失败：物资名称与资产编号不能为空！', 'error')
            return redirect(url_for('inventory'))

        # ✨ 动态裂变：兼容 select 选取和文本框盲打输入新大类/新小类
        category = request.form.get('new_category_name') or request.form.get('category')
        sub_category = request.form.get('new_sub_category_name') or request.form.get('sub_category') or '通用'

        if not category or category.strip() == "":
            flash('入库失败：一级大类绝不能为空！', 'error')
            return redirect(url_for('inventory'))
            
        category = category.strip()
        sub_category = sub_category.strip()

        if not valid_item_text_lengths(name, asset_id, category, sub_category, location, status):
            flash('入库失败：一个或多个字段超过允许长度。', 'error')
            return redirect(url_for('inventory'))

        try:
            if not lock_user_row(current_user().id):
                raise RuntimeError('管理员状态已失效')

            existing_item = Item.query.filter_by(asset_id=asset_id).first()
            if existing_item:
                db.session.rollback()
                flash(f'入库失败：资产编号 {asset_id} 在系统中已存在！', 'error')
                return redirect(url_for('inventory'))

            # 🚀 自动化分类清洗增殖：检测并在库自动生成一级大类
            main_cat = MainCategory.query.filter_by(name=category).first()
            if not main_cat:
                main_cat = MainCategory(name=category)
                db.session.add(main_cat)
                db.session.flush() 

            # 🚀 自动化小类挂载
            sub_cat = SubCategory.query.filter_by(main_id=main_cat.id, name=sub_category).first()
            if not sub_cat and sub_category != '通用':
                sub_cat = SubCategory(name=sub_category, main_id=main_cat.id)
                db.session.add(sub_cat)

            new_item = Item(
                asset_id=asset_id, name=name, category=category,
                sub_category=sub_category, stock=stock, location=location, status=status
            )
            normalize_item_state(new_item)
            db.session.add(new_item)
            db.session.commit()
            
            flash(f'🎉 物资【{name}】成功入库！分类模型自动增殖对齐完成。', 'success')
        except Exception:
            rollback_and_log('create inventory item')
            flash('入库失败，请稍后重试。', 'error')

        return redirect(url_for('inventory'))

    items_list = Item.query.all()
    main_categories = MainCategory.query.all()
    total_items = db.session.query(db.func.sum(Item.stock)).scalar() or 0
    return render_template(
        'inventory.html', active_page='inventory', user_info=current_user(),
        view_mode=session.get('view_mode'), items_list=items_list, main_categories=main_categories,
        category_tree_data=build_category_tree(),
        stats={
            'total_categories': MainCategory.query.count(),
            'total_items': total_items,
            'low_stock_count': Item.query.filter(Item.stock <= Item.min_stock).count(),
            'broken_items_count': Item.query.filter_by(status='急需维修').count()
        }
    )


# ==================== 🛡️ 容灾收容：一级大类安全隔离清除 ====================
@app.route('/admin/main-category/delete/<int:id>', methods=['POST'])
@admin_required
def delete_main_category(id):
    cat = MainCategory.query.get_or_404(id)
    old_name = cat.name
    try:
        # 🌟 安全过滤网：若该分类下尚有物资，一律防清洗强行变轨归拢入“其他”
        affected_items = Item.query.filter_by(category=old_name).all()
        if affected_items:
            fallback_main = MainCategory.query.filter_by(name='其他').first()
            if not fallback_main:
                fallback_main = MainCategory(name='其他')
                db.session.add(fallback_main)
                db.session.flush()
            
            for item in affected_items:
                item.category = '其他'
                item.sub_category = '通用'
        
        db.session.delete(cat)
        db.session.commit()
        flash(f'🗑️ 一级大类【{old_name}】已成功移除，旗下物资已平滑收容转移至【其他】大类！', 'success')
    except Exception:
        rollback_and_log('delete main category')
        flash('大类删除失败，请稍后重试。', 'error')
    return redirect(url_for('inventory'))


# ==================== 🛡️ 容灾收容：二级小类安全隔离清除 ====================
@app.route('/admin/sub-category/delete/<int:id>', methods=['POST'])
@admin_required
def delete_sub_category(id):
    sub = SubCategory.query.get_or_404(id)
    old_sub_name = sub.name
    parent_main_name = sub.main.name if sub.main else '其他'

    try:
        # 🌟 安全变轨：受波及的物资二级细分类统一降维规整为“通用”
        affected_items = Item.query.filter_by(category=parent_main_name, sub_category=old_sub_name).all()
        if affected_items:
            for item in affected_items:
                item.sub_category = '通用'

        db.session.delete(sub)
        db.session.commit()
        flash(f'🗑️ 二级细分【{old_sub_name}】已成功移除，关联物资细分归属已重组为【通用】。', 'success')
    except Exception:
        rollback_and_log('delete sub category')
        flash('小类删除失败，请稍后重试。', 'error')
    return redirect(url_for('inventory'))


@app.route('/admin/item/edit/<int:id>', methods=['POST'])
@admin_required
def edit_item(id):
    item = db.get_or_404(Item, id)
    name = request.form.get('name', '').strip()
    asset_id = request.form.get('asset_id', '').strip()
    if not name or not asset_id:
        flash('更新失败：物资名称与资产编号不能为空！', 'error')
        return redirect(url_for('inventory'))

    duplicate = Item.query.filter(Item.asset_id == asset_id, Item.id != id).first()
    if duplicate:
        flash(f'更新失败：资产编号 {asset_id} 已被其他物资占用！', 'error')
        return redirect(url_for('inventory'))

    category = request.form.get('category', '').strip() or '其他'
    sub_category = request.form.get('sub_category', '').strip() or '通用'
    location = request.form.get('location', '').strip() or '未知库区'
    status = request.form.get('status', '').strip() or '运行中'
    if not valid_item_text_lengths(
        name,
        asset_id,
        category,
        sub_category,
        location,
        status,
    ):
        flash('更新失败：一个或多个字段超过允许长度。', 'error')
        return redirect(url_for('inventory'))

    item.name = name
    item.asset_id = asset_id
    item.category = category
    item.sub_category = sub_category
    item.location = location
    item.stock = parse_non_negative_int(request.form.get('stock'), 0)
    item.status = status
    normalize_item_state(item)
    
    try:
        db.session.commit()
        flash(f'✨ 物资【{item.name}】全要素配置更新成功！', 'success')
    except Exception:
        rollback_and_log('update inventory item')
        flash('更新失败，请稍后重试。', 'error')
    return redirect(url_for('inventory'))

@app.route('/admin/item/delete/<int:id>', methods=['POST'])
@admin_required
def delete_item(id):
    item = db.get_or_404(Item, id)
    name = item.name
    related_records = BorrowRecord.query.filter_by(item_id=id).count() + BorrowDetail.query.filter_by(item_id=id).count()
    if related_records:
        flash(f'删除失败：物资【{name}】已有借用/审批流水，请保留历史记录或先完成单据闭环。', 'error')
        return redirect(url_for('inventory'))
    try:
        db.session.delete(item)
        db.session.commit()
        flash(f'🗑️ 物资【{name}】已成功从系统库中永久移除！', 'success')
    except Exception:
        rollback_and_log('delete inventory item')
        flash('删除失败，请稍后重试。', 'error')
    return redirect(url_for('inventory'))

@app.route('/admin/inventory/export')
@admin_required
def export_inventory():
    items = Item.query.all()
    si = io.StringIO()
    si.write('\ufeff')
    cw = csv.writer(si)
    cw.writerow(['资产名称', '资产编号', '一级大类', '二级小类', '存放位置', '当前在库数量', '实时状态'])
    for item in items:
        cw.writerow([
            csv_safe_cell(item.name),
            csv_safe_cell(item.asset_id),
            csv_safe_cell(item.category),
            csv_safe_cell(item.sub_category),
            csv_safe_cell(item.location),
            item.stock,
            csv_safe_cell(item.status),
        ])
    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = "attachment; filename=inventory_report.csv"
    output.headers["Content-type"] = "text/csv; charset=utf-8"
    return output

@app.route('/admin/inventory/import', methods=['POST'])
@admin_required
def import_inventory():
    file = request.files.get('file')
    if not file or not file.filename.lower().endswith('.csv'):
        flash('导入失败：请上传标准的 .csv 表格文件！', 'error')
        return redirect(url_for('inventory'))
    try:
        stream = io.StringIO(decode_csv_upload(file), newline=None)
        csv_input = csv.reader(stream)
        next(csv_input, None)
        parsed_rows = []
        seen_file_asset_ids = set()
        skip_count = 0
        for row in csv_input:
            if len(row) < 6:
                skip_count += 1
                continue
            name = row[0].strip()
            asset_id = row[1].strip()
            category = row[2].strip()
            sub_category = row[3].strip() or '通用'
            location = row[4].strip() or '未知库区'
            stock = row[5].strip()
            status = row[6].strip() if len(row) > 6 and row[6].strip() else "库存充足"
            if (
                not name
                or not asset_id
                or not category
                or asset_id in seen_file_asset_ids
                or not valid_item_text_lengths(
                    name,
                    asset_id,
                    category,
                    sub_category,
                    location,
                    status,
                )
            ):
                skip_count += 1
                continue
            seen_file_asset_ids.add(asset_id)
            parsed_rows.append({
                'name': name,
                'asset_id': asset_id,
                'category': category,
                'sub_category': sub_category,
                'location': location,
                'stock': parse_non_negative_int(stock, 0),
                'status': status,
            })

        if not lock_user_row(current_user().id):
            raise RuntimeError('管理员状态已失效')

        existing_asset_ids = set()
        uploaded_asset_ids = [row['asset_id'] for row in parsed_rows]
        for offset in range(0, len(uploaded_asset_ids), 500):
            chunk = uploaded_asset_ids[offset:offset + 500]
            existing_asset_ids.update(
                asset_id
                for asset_id, in db.session.query(Item.asset_id)
                .filter(Item.asset_id.in_(chunk))
                .all()
            )

        candidate_rows = []
        for row in parsed_rows:
            if row['asset_id'] in existing_asset_ids:
                skip_count += 1
            else:
                candidate_rows.append(row)

        main_categories = {category.name: category for category in MainCategory.query.all()}
        new_main_categories = [
            MainCategory(name=name)
            for name in sorted({row['category'] for row in candidate_rows} - main_categories.keys())
        ]
        if new_main_categories:
            db.session.add_all(new_main_categories)
            db.session.flush()
            main_categories.update({category.name: category for category in new_main_categories})

        sub_category_keys = {
            (sub_category.main_id, sub_category.name)
            for sub_category in SubCategory.query.all()
        }
        new_sub_categories = []
        for row in candidate_rows:
            if row['sub_category'] == '通用':
                continue
            key = (main_categories[row['category']].id, row['sub_category'])
            if key not in sub_category_keys:
                sub_category_keys.add(key)
                new_sub_categories.append(SubCategory(name=key[1], main_id=key[0]))
        if new_sub_categories:
            db.session.add_all(new_sub_categories)

        new_items = []
        for row in candidate_rows:
            item = Item(
                name=row['name'],
                asset_id=row['asset_id'],
                category=row['category'],
                sub_category=row['sub_category'],
                location=row['location'],
                stock=row['stock'],
                status=row['status'],
            )
            normalize_item_state(item)
            new_items.append(item)
        if new_items:
            db.session.add_all(new_items)
        db.session.commit()
        flash(f'📋 批量导入成功！成功追加 {len(new_items)} 项物资，跳过 {skip_count} 项无效或重复数据。', 'success')
    except Exception:
        rollback_and_log('import inventory CSV')
        flash('导入失败，请检查表格编码、列格式和字段长度。', 'error')
    return redirect(url_for('inventory'))

@app.route('/admin/main-category/add', methods=['POST'])
@admin_required
def add_main_category():
    name = request.form.get('main_category_name', '').strip()
    if not name or len(name) > 50:
        flash('大类名称不能为空且不能超过 50 个字符。', 'error')
        return redirect(url_for('inventory'))
    if MainCategory.query.filter_by(name=name).first():
        flash(f'大类【{name}】已存在！', 'error')
    else:
        try:
            db.session.add(MainCategory(name=name))
            db.session.commit()
            flash(f'🎉 成功组建一级大类【{name}】！', 'success')
        except Exception:
            rollback_and_log('create main category')
            flash('大类创建失败，请稍后重试。', 'error')
    return redirect(url_for('inventory'))


@app.route('/audit', methods=['GET'])
@admin_required
def audit():
    records_list = borrow_records_with_related(
        BorrowRecord.query.order_by(BorrowRecord.borrow_date.desc())
    ).all()
    total_pending = BorrowRecord.query.filter(BorrowRecord.status.in_(['等待审批', '待归还审核'])).count()
    
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_processed = BorrowRecord.query.filter(
        BorrowRecord.status.in_(['进行中', '已拒绝', '已归还']), 
        BorrowRecord.borrow_date >= today_start
    ).count()

    admin_info = current_user()
    return render_template(
        'audit.html', active_page='audit', user_info=admin_info, view_mode=session.get('view_mode'), items_list=records_list,
        stats={'total_categories': total_pending, 'total_items': today_processed, 'low_stock_count': 0, 'broken_items_count': 0}
    )


def record_item_quantities(record):
    quantities = db.session.query(
        BorrowDetail.item_id,
        db.func.sum(BorrowDetail.quantity),
    ).filter_by(record_id=record.id).group_by(
        BorrowDetail.item_id
    ).order_by(BorrowDetail.item_id).all()
    if quantities:
        return {item_id: int(quantity or 0) for item_id, quantity in quantities}
    return {record.item_id: 1}


def decrement_item_stock(item_id, quantity):
    if quantity <= 0:
        return False
    stock_after = Item.stock - quantity
    result = db.session.execute(
        update(Item)
        .where(
            Item.id == item_id,
            Item.stock >= quantity,
            ~db.func.coalesce(Item.status, '').like('%维修%'),
        )
        .values(
            stock=stock_after,
            status=case((stock_after <= 0, '无库存'), else_='库存充足'),
            status_color=case((stock_after <= 0, 'error'), else_='emerald'),
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def increment_item_stock(item_id, quantity):
    if quantity <= 0:
        return False
    stock_after = Item.stock + quantity
    item_is_broken = db.func.coalesce(Item.status, '').like('%维修%')
    result = db.session.execute(
        update(Item)
        .where(Item.id == item_id)
        .values(
            stock=stock_after,
            status=case((item_is_broken, '急需维修'), else_='库存充足'),
            status_color=case((item_is_broken, 'error'), else_='emerald'),
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


# 🌟 终极强悍核心控制总线：一套业务总线，同时完美处理【借出审批】与【归还入库核销】
@app.route('/admin/audit/handle/<int:id>', methods=['POST'])
@admin_required
def handle_audit(id):
    record = db.get_or_404(BorrowRecord, id)
    action = request.form.get('action')

    if action not in ['进行中', '已拒绝', '已归还']:
        flash('签批失败：未知的审批决断动作！', 'error')
        return redirect(url_for('audit'))

    if record.status == '等待审批' and action not in {'进行中', '已拒绝'}:
        flash('借出申请只能批准或驳回。', 'error')
        return redirect(url_for('audit'))
    if record.status == '待归还审核' and action not in {'已归还', '进行中'}:
        flash('归还申请只能确认入库或退回借用中。', 'error')
        return redirect(url_for('audit'))
    if record.status not in {'等待审批', '待归还审核'}:
        flash('该单据当前状态不需要进行任何审批操作！', 'error')
        return redirect(url_for('audit'))

    original_status = record.status
    try:
        values = {'status': action}
        if original_status == '待归还审核' and action == '已归还':
            values['actual_return_date'] = datetime.now()

        claim = db.session.execute(
            update(BorrowRecord)
            .where(BorrowRecord.id == record.id, BorrowRecord.status == original_status)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if claim.rowcount != 1:
            db.session.rollback()
            flash('该单据已被其他管理员处理，请刷新后查看。', 'warning')
            return redirect(url_for('audit'))

        if original_status == '等待审批' and action == '进行中':
            quantities = record_item_quantities(record)
            if not all(decrement_item_stock(item_id, quantity) for item_id, quantity in quantities.items()):
                db.session.rollback()
                flash('审批中止：部分物资库存不足、处于维修状态或已不存在。', 'error')
                return redirect(url_for('audit'))
        elif original_status == '待归还审核' and action == '已归还':
            quantities = record_item_quantities(record)
            if not all(increment_item_stock(item_id, quantity) for item_id, quantity in quantities.items()):
                db.session.rollback()
                flash('归还失败：单据中的部分物资已不存在。', 'error')
                return redirect(url_for('audit'))

        db.session.commit()
        if original_status == '等待审批':
            message = (
                f'🎉 单据 #REQ-2026-00{record.id} 签批成功！'
                if action == '进行中'
                else '🗑️ 单据已被驳回。'
            )
            flash(message, 'success' if action == '进行中' else 'warning')
        elif action == '已归还':
            flash(f'📦 单据 #REQ-2026-00{record.id} 归还核验成功，全套物资已重新入库！', 'success')
        else:
            flash(f'⚠️ 单据 #REQ-2026-00{record.id} 已退回借用中状态。', 'warning')
    except Exception:
        rollback_and_log('handle borrow audit')
        flash('审批处理失败，请稍后重试。', 'error')

    return redirect(url_for('audit'))



@app.route('/admin/sub-category/add', methods=['POST'])
@admin_required
def add_sub_category():
    # 1. 提取前端动态 DOM 表单提交上来的核心字段
    main_id = request.form.get('main_category_id')
    sub_name = request.form.get('sub_category_name', '').strip()
    
    if not main_id or not sub_name or len(sub_name) > 50:
        flash('添加失败：小分类名称不能为空且不能超过 50 个字符。', 'error')
        return redirect(url_for('inventory'))

    try:
        # 2. 查重机制：防止在该大类下重复添加一模一样的小类
        existing = SubCategory.query.filter_by(main_id=main_id, name=sub_name).first()
        if existing:
            flash(f'添加失败：该大类下已存在小类细分【{sub_name}】！', 'error')
        else:
            # 3. 灌入数据库
            new_sub = SubCategory(name=sub_name, main_id=main_id)
            db.session.add(new_sub)
            db.session.commit()
            flash(f'二级细分【{sub_name}】成功挂载至节点！', 'success')
            
    except Exception:
        rollback_and_log('create sub category')
        flash('小分类创建失败，请稍后重试。', 'error')
        
    return redirect(url_for('inventory'))


def ensure_database_indexes():
    indexes = sorted(
        BorrowRecord.__table__.indexes | BorrowDetail.__table__.indexes,
        key=lambda index: index.name,
    )
    if db.engine.dialect.name == 'sqlite':
        with db.engine.begin() as connection:
            for index in indexes:
                connection.execute(CreateIndex(index, if_not_exists=True))
    else:
        for index in indexes:
            index.create(bind=db.engine, checkfirst=True)


def upgrade_legacy_passwords():
    upgraded_count = 0
    for user in User.query.all():
        if user.password.startswith(('pbkdf2:', 'scrypt:')):
            continue
        if len(user.password) > 128:
            app.logger.warning('用户 %s 的密码格式未知，未自动迁移。', user.id)
            continue
        user.password = generate_password_hash(
            user.password,
            method='pbkdf2:sha256',
            salt_length=16,
        )
        upgraded_count += 1
    if upgraded_count:
        db.session.commit()
        app.logger.info('已迁移 %s 个历史明文密码。', upgraded_count)


def init_db():
    with app.app_context():
        db.create_all()
        ensure_database_indexes()
        seed_demo_data = app.config['SEED_DEMO_DATA']

        if seed_demo_data and MainCategory.query.count() == 0:
            cat_board = MainCategory(name='开发板')
            cat_office = MainCategory(name='办公电子')
            cat_machinery = MainCategory(name='工程机械')
            cat_it = MainCategory(name='IT硬件')  
            db.session.add_all([cat_board, cat_office, cat_machinery, cat_it])
            db.session.commit() 
            db.session.add_all([
                SubCategory(name='STM32', main_id=cat_board.id), SubCategory(name='ESP32', main_id=cat_board.id), SubCategory(name='OpenMV', main_id=cat_board.id),
                SubCategory(name='显示器', main_id=cat_office.id), SubCategory(name='充电周边', main_id=cat_office.id), SubCategory(name='笔记本电脑', main_id=cat_office.id),
                SubCategory(name='无刷电机驱动板', main_id=cat_machinery.id), SubCategory(name='服务器', main_id=cat_it.id)          
            ])
            db.session.commit()

        if User.query.count() == 0:
            bootstrap_password = os.environ.get('BOOTSTRAP_ADMIN_PASSWORD', '')
            if bootstrap_password:
                bootstrap_username = os.environ.get('BOOTSTRAP_ADMIN_USERNAME', 'admin').strip()
                bootstrap_name = os.environ.get('BOOTSTRAP_ADMIN_NAME', 'System Administrator').strip()
                if (
                    not bootstrap_username
                    or not bootstrap_name
                    or len(bootstrap_username) > 50
                    or len(bootstrap_name) > 50
                    or not 12 <= len(bootstrap_password) <= 128
                ):
                    raise RuntimeError('初始管理员字段无效，密码长度须为 12–128 位。')
                db.session.add(User(
                    username=bootstrap_username,
                    password=generate_password_hash(
                        bootstrap_password,
                        method='pbkdf2:sha256',
                        salt_length=16,
                    ),
                    full_name=bootstrap_name,
                    role='admin',
                ))
                db.session.commit()
            elif seed_demo_data:
                secure_admin_password = generate_password_hash('123456', method='pbkdf2:sha256', salt_length=16)
                admin_user = User(username='admin', password=secure_admin_password, full_name='Alex Chen', role='admin')
                default_user_password = generate_password_hash('123456', method='pbkdf2:sha256', salt_length=16)
                normal_user1 = User(username='user1', password=default_user_password, full_name='张三', role='user', credit_score=98, quota_limit=20)
                normal_user2 = User(username='user2', password=default_user_password, full_name='李四', role='user', credit_score=72, quota_limit=15)
                db.session.add_all([admin_user, normal_user1, normal_user2])
                db.session.commit()
            elif APP_ENV == 'production':
                raise RuntimeError('空数据库必须设置 BOOTSTRAP_ADMIN_PASSWORD 以创建初始管理员。')
            else:
                app.logger.warning('数据库中没有用户；请注册用户或设置 BOOTSTRAP_ADMIN_PASSWORD。')

        upgrade_legacy_passwords()

        if seed_demo_data and Item.query.count() == 0:
            target_user = User.query.filter_by(username='user1').first()
            if target_user:
                item1 = Item(asset_id="AST-2026-001", name="UltraSharp 32寸显示器", category="办公电子", sub_category="显示器", status="运行中", status_color="emerald", location="A座 3楼 办公区", stock=12, min_stock=2)
                item2 = Item(asset_id="AST-2026-002", name="65W 氮化镓充电头", category="办公电子", sub_category="充电周边", status="运行中", status_color="emerald", location="B座 电子阅览室", stock=5, min_stock=6)
                item3 = Item(asset_id="AST-2026-009", name="Dell PowerEdge R750", category="IT硬件", sub_category="服务器", status="运行中", status_color="emerald", location="核心机房 03柜", stock=2, min_stock=1)
                item4 = Item(asset_id="AST-2026-118", name="高精度机械臂 v4", category="工程机械", sub_category="无刷电机驱动板", status="急需维修", status_color="error", location="南区智能实验室", stock=1, min_stock=1)
                for item in (item1, item2, item3, item4):
                    normalize_item_state(item)
                db.session.add_all([item1, item2, item3, item4])
                db.session.commit()

                record1 = BorrowRecord(user_id=target_user.id, item_id=item1.id, return_date=datetime.now()+timedelta(days=7), reason="实验室调试设备", status='等待审批', borrow_date=datetime.now())
                db.session.add(record1); db.session.flush()
                db.session.add(BorrowDetail(record_id=record1.id, item_id=item1.id, quantity=1))

                record2 = BorrowRecord(user_id=target_user.id, item_id=item2.id, return_date=datetime.now()+timedelta(days=5), reason="日常外设补充", status='进行中', borrow_date=datetime.now() - timedelta(days=3))
                db.session.add(record2); db.session.flush()
                db.session.add(BorrowDetail(record_id=record2.id, item_id=item2.id, quantity=2))

                db.session.commit()
                if os.environ.get('QUIET_INIT_DB') != '1':
                    print("🎉 活数据库存闭环流水线全部组装成功！")

if os.environ.get('AUTO_INIT_DB', '1') != '0':
    init_db()

def run_dev_server():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=os.environ.get('FLASK_DEBUG') == '1')


if __name__ == '__main__':
    run_dev_server()
