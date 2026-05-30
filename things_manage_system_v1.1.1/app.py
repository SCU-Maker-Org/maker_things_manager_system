from flask import Flask, render_template, request, flash, redirect, url_for, session, jsonify, make_response
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
import csv
import io
from werkzeug.security import generate_password_hash, check_password_hash


app = Flask(__name__)

# 🔑 设置秘钥，有了它 session 才能正常工作，用来在浏览器 Cookie 里加密存状态
app.secret_key = 'super_secret_key_for_testing'

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///storage.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
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
    id = db.Column(db.Integer, primary_key=True)
    record_id = db.Column(db.Integer, db.ForeignKey('borrow_records.id'), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey('items.id'), nullable=False)
    quantity = db.Column(db.Integer, default=1)                          # 借用数量
    
    item = db.relationship('Item', backref='borrow_details', lazy=True)


# ==================== 🐍 路由与身份分流控制 ====================

# ==================== 🐍 身份验证流（登录与安全注册） ====================

@app.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        user = User.query.filter_by(username=email).first()
        
        # 🌟 核心安全修复：使用 check_password_hash 进行密文哈希比对
        if user and (check_password_hash(user.password, password) or user.password == password): 
            # 注：后面的 'or user.password == password' 是为了兼容你 init_db 里写死的明文老测试账号 '123456'
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
    except Exception as e:
        db.session.rollback()
        flash(f'系统错误，注册落库失败：{str(e)}', 'error')

    return redirect(url_for('login'))


@app.route('/switch-mode')
def switch_mode():
    if session.get('role') != 'admin':
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
def admin_dashboard():
    if session.get('view_mode') == 'user':
        return redirect(url_for('user_dashboard'))

    total_items = Item.query.count()
    active_borrows = BorrowRecord.query.filter_by(status='进行中').count()
    pending_approvals = BorrowRecord.query.filter(BorrowRecord.status.in_(['等待审批', '待归还审核'])).count()
    error_items = Item.query.filter_by(status='急需维修').count()

    recent_borrows = BorrowRecord.query.order_by(BorrowRecord.borrow_date.desc()).limit(5).all()
    users_list = User.query.filter_by(role='user').order_by(User.credit_score.asc()).limit(4).all()
    
    admin_info = db.session.get(User, session.get('user_id', 1))
    
    return render_template(
        'admin_dashboard.html',
        active_page='dashboard',
        user_info=admin_info,
        view_mode=session.get('view_mode'), 
        total_items=total_items,
        active_borrows=active_borrows,
        pending_approvals=pending_approvals,
        error_items=error_items,
        recent_borrows=recent_borrows,
        users_list=users_list
    )


@app.route('/user/dashboard')
def user_dashboard():
    current_user_id = session.get('user_id', 2) 
    user_info = db.session.get(User, current_user_id)
    my_active_count = BorrowRecord.query.filter_by(user_id=current_user_id, status='进行中').count()
    my_pending_count = BorrowRecord.query.filter_by(user_id=current_user_id, status='等待审批').count()
    my_borrows = BorrowRecord.query.filter_by(user_id=current_user_id).order_by(BorrowRecord.borrow_date.desc()).limit(5).all()

    return render_template(
        'user_dashboard.html',
        active_page='dashboard',
        user_info=user_info,
        view_mode=session.get('view_mode'), 
        my_active_count=my_active_count,
        my_pending_count=my_pending_count,
        my_borrows=my_borrows
    )

@app.route('/catalog')
def catalog():
    current_user_id = session.get('user_id', 2)
    user_info = db.session.get(User, current_user_id)
    
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
def submit_batch_borrow():
    current_user_id = session.get('user_id')
    if not current_user_id: 
        return jsonify({'success': False, 'msg': '登录会话失效，请重新登录系统！'}), 401
        
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'msg': '未接收到有效的数据传输包！'}), 400

    reason = data.get('reason', '').strip()
    return_date_str = data.get('return_date')
    cart_items = data.get('items', [])

    if not reason or not return_date_str or not cart_items:
        return jsonify({'success': False, 'msg': '合并失败：请补全用途事由或预计归还日期！'}), 400

    try:
        return_date = datetime.strptime(return_date_str, '%Y-%m-%d')
        
        # 抓取第一件物资的 ID 用作老模型 item_id 的强制无损抗瘫痪兼容垫底
        fallback_item_id = int(cart_items[0].get('item_id'))

        # 录入借用单主表记录
        new_record = BorrowRecord(
            user_id=current_user_id, 
            item_id=fallback_item_id, 
            reason=reason, 
            return_date=return_date, 
            status='等待审批'
        )
        db.session.add(new_record)
        db.session.flush() 

        # 循环灌入明细子表
        for chunk in cart_items:
            item_id = int(chunk.get('item_id'))
            req_qty = int(chunk.get('quantity', 1))
            
            target_stock = db.session.get(Item, item_id)
            if not target_stock or target_stock.stock < req_qty:
                return jsonify({
                    'success': False, 
                    'msg': f'【{target_stock.name if target_stock else item_id}】库存水位告急，剩余可借 {target_stock.stock if target_stock else 0} 件'
                }), 400
            
            db.session.add(BorrowDetail(
                record_id=new_record.id, 
                item_id=item_id, 
                quantity=req_qty
            ))

        db.session.commit()
        return jsonify({'success': True, 'msg': '您的合并批量申请单已成功挂载至决策中心，请等待审批！'})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'msg': f'系统底层异常: {str(e)}'}), 500

@app.route('/borrow', methods=['GET'])
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
    my_borrows = BorrowRecord.query.filter_by(user_id=current_user_id)\
                                   .order_by(BorrowRecord.borrow_date.desc())\
                                   .all()

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

    if record.status != '等待审批':
        return jsonify({'success': False, 'msg': '撤回失败：该单据管理员已在处理中，无法取消！'}), 400

    try:
        db.session.delete(record)
        db.session.commit()
        return jsonify({'success': True, 'msg': '🎉 申请单据已成功撤回，暂存额度已即时返还！'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'msg': f'服务器底层发生异常：{str(e)}'}), 500


@app.route('/api/borrow/return/<int:id>', methods=['POST'])
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

    if record.status != '进行中':
        return jsonify({'success': False, 'msg': '该单据当前状态无法执行归还操作！'}), 400

    try:
        record.status = '待归还审核'
        db.session.commit()
        return jsonify({'success': True, 'msg': '🎉 归还申请已提交！请将物资放回原处，等待管理员审核入库。'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'msg': f'服务器底层发生异常：{str(e)}'}), 500


@app.route('/setting')
def setting(): return "<h3>系统设置页面正在全力开发中...</h3><br><a href='javascript:history.back()'>返回上一页</a>"

@app.route('/logout')
def logout():
    session.clear()  
    flash('您已成功安全退出登录', 'success')
    return redirect(url_for('login'))

@app.route('/person')
def person(): return "<h3>个人中心页面（开发中...）</h3><br><a href='javascript:history.back()'>返回上一页</a>"


# ==================== 📦 动态自适应智能入库大盘总线 ====================
@app.route('/inventory', methods=['GET', 'POST'])
def inventory():
    if session.get('role') != 'admin' or session.get('view_mode') == 'user':
        flash('您当前处于用户视图，无权访问库存管理后台！', 'error')
        return redirect(url_for('user_dashboard'))

    if request.method == 'POST':
        name = request.form.get('name')
        asset_id = request.form.get('asset_id')
        location = request.form.get('location') or "未知库区"
        stock = int(request.form.get('stock') or 0)
        status = request.form.get('status') or "库存充足"

        # ✨ 动态裂变：兼容 select 选取和文本框盲打输入新大类/新小类
        category = request.form.get('category') or request.form.get('new_category_name')
        sub_category = request.form.get('sub_category') or request.form.get('new_sub_category_name') or '通用'

        if not category or category.strip() == "":
            flash('入库失败：一级大类绝不能为空！', 'error')
            return redirect(url_for('inventory'))
            
        category = category.strip()
        sub_category = sub_category.strip()

        existing_item = Item.query.filter_by(asset_id=asset_id).first()
        if existing_item:
            flash(f'入库失败：资产编号 {asset_id} 在系统中已存在！', 'error')
            return redirect(url_for('inventory'))

        try:
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
            db.session.add(new_item)
            db.session.commit()
            
            flash(f'🎉 物资【{name}】成功入库！分类模型自动增殖对齐完成。', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'入库失败，数据库事务异常：{str(e)}', 'error')

        return redirect(url_for('inventory'))

    items_list = Item.query.all()
    main_categories = MainCategory.query.all()
    total_items = db.session.query(db.func.sum(Item.stock)).scalar() or 0
    return render_template(
        'inventory.html', active_page='inventory', user_info=db.session.get(User, session.get('user_id', 1)), 
        view_mode=session.get('view_mode'), items_list=items_list, main_categories=main_categories,
        stats={'total_categories': MainCategory.query.count(), 'total_items': total_items, 'low_stock_count': Item.query.filter(Item.stock == 0).count(), 'broken_items_count': Item.query.filter_by(status='急需维修').count()}
    )


# ==================== 🛡️ 容灾收容：一级大类安全隔离清除 ====================
@app.route('/admin/main-category/delete/<int:id>', methods=['POST'])
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
    except Exception as e:
        db.session.rollback()
        flash(f'大类清洗操作失败：{str(e)}', 'error')
    return redirect(url_for('inventory'))


# ==================== 🛡️ 容灾收容：二级小类安全隔离清除 ====================
@app.route('/admin/sub-category/delete/<int:id>', methods=['POST'])
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
    except Exception as e:
        db.session.rollback()
        flash(f'小类清洗操作失败：{str(e)}', 'error')
    return redirect(url_for('inventory'))


@app.route('/admin/item/edit/<int:id>', methods=['POST'])
def edit_item(id):
    item = db.get_or_404(Item, id)
    item.name = request.form.get('name')
    item.asset_id = request.form.get('asset_id')
    item.category = request.form.get('category')
    item.sub_category = request.form.get('sub_category')
    item.location = request.form.get('location')
    item.stock = int(request.form.get('stock') or 0)
    item.status = request.form.get('status')
    
    db.session.commit()
    flash(f'✨ 物资【{item.name}】全要素配置更新成功！', 'success')
    return redirect(url_for('inventory'))

@app.route('/admin/item/delete/<int:id>', methods=['POST'])
def delete_item(id):
    item = db.get_or_404(Item, id)
    name = item.name
    db.session.delete(item)
    db.session.commit()
    flash(f'🗑️ 物资【{name}】已成功从系统库中永久移除！', 'success')
    return redirect(url_for('inventory'))

@app.route('/admin/inventory/export')
def export_inventory():
    items = Item.query.all()
    si = io.StringIO()
    si.write('\ufeff')
    cw = csv.writer(si)
    cw.writerow(['资产名称', '资产编号', '一级大类', '二级小类', '存放位置', '当前在库数量', '实时状态'])
    for item in items:
        cw.writerow([item.name, item.asset_id, item.category, item.sub_category, item.location, item.stock, item.status])
    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = "attachment; filename=inventory_report.csv"
    output.headers["Content-type"] = "text/csv; charset=utf-8"
    return output

@app.route('/admin/inventory/import', methods=['POST'])
def import_inventory():
    file = request.files.get('file')
    if not file or not file.filename.endswith('.csv'):
        flash('导入失败：请上传标准的 .csv 表格文件！', 'error')
        return redirect(url_for('inventory'))
    try:
        stream = io.StringIO(file.stream.read().decode("utf-8"), newline=None)
        if stream.getvalue().startswith('\ufeff'):
            stream = io.StringIO(stream.getvalue().lstrip('\ufeff'))
        csv_input = csv.reader(stream)
        next(csv_input)
        success_count, skip_count = 0, 0
        for row in csv_input:
            if len(row) < 6: continue
            name, asset_id, category, sub_category, location, stock = row[0].strip(), row[1].strip(), row[2].strip(), row[3].strip(), row[4].strip(), row[5].strip()
            status = row[6].strip() if len(row) > 6 else "库存充足"
            if Item.query.filter_by(asset_id=asset_id).first():
                skip_count += 1
                continue
            db.session.add(Item(name=name, asset_id=asset_id, category=category, sub_category=sub_category, location=location, stock=int(stock or 0), status=status))
            success_count += 1
        db.session.commit()
        flash(f'📋 批量导入成功！成功追加 {success_count} 项物资，因编号重复跳过 {skip_count} 项。', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'导入解析发生崩溃，请检查表格编码或列格式：{str(e)}', 'error')
    return redirect(url_for('inventory'))

@app.route('/admin/main-category/add', methods=['POST'])
def add_main_category():
    name = request.form.get('main_category_name', '').strip()
    if name:
        if MainCategory.query.filter_by(name=name).first():
            flash(f'大类【{name}】已存在！', 'error')
        else:
            db.session.add(MainCategory(name=name))
            db.session.commit()
            flash(f'🎉 成功组建一级大类【{name}】！', 'success')
    return redirect(url_for('inventory'))


@app.route('/audit', methods=['GET'])
def audit():
    if session.get('role') != 'admin' or session.get('view_mode') == 'user':
        flash('您当前处于普通用户视图，无权访问审批决策控制台！', 'error')
        return redirect(url_for('user_dashboard'))

    records_list = BorrowRecord.query.order_by(BorrowRecord.borrow_date.desc()).all()
    total_pending = BorrowRecord.query.filter(BorrowRecord.status.in_(['等待审批', '待归还审核'])).count()
    
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_processed = BorrowRecord.query.filter(
        BorrowRecord.status.in_(['进行中', '已拒绝', '已归还']), 
        BorrowRecord.borrow_date >= today_start
    ).count()

    admin_info = db.session.get(User, session.get('user_id', 1))
    return render_template(
        'audit.html', active_page='audit', user_info=admin_info, view_mode=session.get('view_mode'), items_list=records_list,
        stats={'total_categories': total_pending, 'total_items': today_processed, 'low_stock_count': 0, 'broken_items_count': 0}
    )


# 🌟 终极强悍核心控制总线：一套业务总线，同时完美处理【借出审批】与【归还入库核销】
@app.route('/admin/audit/handle/<int:id>', methods=['POST'])
def handle_audit(id):
    if session.get('role') != 'admin':
        flash('安全拦截：非管理员非法越权提交签批指令！', 'error')
        return redirect(url_for('user_dashboard'))

    record = db.get_or_404(BorrowRecord, id)
    action = request.form.get('action')          
    remark = request.form.get('remark', '').strip() 

    if action not in ['进行中', '已拒绝', '已归还']:
        flash('签批失败：未知的审批决断动作！', 'error')
        return redirect(url_for('audit'))

    # ------------------ 🟩 场景一：处理【借出申请】 ------------------
    if record.status == '等待审批':
        if action == '进行中':
            if record.details:
                for detail in record.details:
                    target_item = detail.item
                    if target_item:
                        if target_item.stock < detail.quantity:
                            flash(f'审批中止：物资【{target_item.name}】实际库存不足以支付本次申请的 {detail.quantity} 件需求！', 'error')
                            return redirect(url_for('audit'))
                        target_item.stock -= detail.quantity
                        if target_item.stock == 0:
                            target_item.status, target_item.status_color = '无库存', 'error'
            else:
                target_item = db.session.get(Item, record.item_id)
                if target_item:
                    if target_item.stock < 1:
                        flash(f'审批中止：物资【{target_item.name}】当前在库数量为 0！', 'error')
                        return redirect(url_for('audit'))
                    target_item.stock -= 1
                    if target_item.stock == 0:
                        target_item.status, target_item.status_color = '无库存', 'error'

        record.status = action
        db.session.commit()
        flash(f'🎉 单据 #REQ-2026-00{record.id} 签批成功！' if action == '进行中' else f'🗑️ 单据已被驳回。', 'success' if action == '进行中' else 'warning')

    # ------------------ 🟪 场景二：处理【归还申请】 ------------------
    elif record.status == '待归还审核':
        if action == '已归还':
            # ✨ 级联恢复：原路精准扣减借出的负重，放回在库大盘
            if record.details:
                for detail in record.details:
                    target_item = detail.item
                    if target_item:
                        target_item.stock += detail.quantity
                        if target_item.status == '无库存':
                            target_item.status, target_item.status_color = '运行中', 'emerald'
            else:
                target_item = db.session.get(Item, record.item_id)
                if target_item:
                    target_item.stock += 1
                    if target_item.status == '无库存':
                        target_item.status, target_item.status_color = '运行中', 'emerald'
            
            record.status = '已归还'
            record.actual_return_date = datetime.now() # 历史归档归还时间戳写入
            db.session.commit()
            flash(f'📦 单据 #REQ-2026-00{record.id} 归还核验成功，全套物资已重新入库！', 'success')
            
        elif action == '进行中':
            # 管理员选择异常驳回（少件、物料受损），驳回请求，单据重新退回持用进行中状态
            record.status = '进行中'
            db.session.commit()
            flash(f'⚠️ 已异常驳回该归还申请！单据 #REQ-2026-00{record.id} 已恢复为借用中状态。', 'warning')
    else:
        flash('该单据当前状态不需要进行任何审批操作！', 'error')

    return redirect(url_for('audit'))



@app.route('/admin/sub-category/add', methods=['POST'])
def add_sub_category():
    if session.get('role') != 'admin':
        flash('安全拦截：非管理员无权添加分类标签！', 'error')
        return redirect(url_for('user_dashboard'))

    # 1. 提取前端动态 DOM 表单提交上来的核心字段
    main_id = request.form.get('main_category_id')
    sub_name = request.form.get('sub_category_name', '').strip()
    
    if not main_id or not sub_name:
        flash('添加失败：小分类名称不能为空！', 'error')
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
            
    except Exception as e:
        db.session.rollback()
        flash(f'底层数据库事务异常：{str(e)}', 'error')
        
    return redirect(url_for('inventory'))

def init_db():
    with app.app_context():
        db.create_all()
        if MainCategory.query.count() == 0:
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
            secure_admin_password = generate_password_hash('123456', method='pbkdf2:sha256', salt_length=16)
            admin_user = User(username='admin', password=secure_admin_password, full_name='Alex Chen', role='admin')
            normal_user1 = User(username='user1', password='123456', full_name='张三', role='user', credit_score=98, quota_limit=20)
            normal_user2 = User(username='user2', password='123456', full_name='李四', role='user', credit_score=72, quota_limit=15)
            db.session.add_all([admin_user, normal_user1, normal_user2])
            db.session.commit()

        if Item.query.count() == 0:
            target_user = User.query.filter_by(username='user1').first()
            user_id_badge = target_user.id if target_user else 2
            
            item1 = Item(asset_id="AST-2026-001", name="UltraSharp 32寸显示器", category="办公电子", sub_category="显示器", status="运行中", status_color="emerald", location="A座 3楼 办公区", stock=12, min_stock=2)
            item2 = Item(asset_id="AST-2026-002", name="65W 氮化镓充电头", category="办公电子", sub_category="充电周边", status="运行中", status_color="emerald", location="B座 电子阅览室", stock=5, min_stock=6) 
            item3 = Item(asset_id="AST-2026-009", name="Dell PowerEdge R750", category="IT硬件", sub_category="服务器", status="运行中", status_color="emerald", location="核心机房 03柜", stock=2, min_stock=1)
            item4 = Item(asset_id="AST-2026-118", name="高精度机械臂 v4", category="工程机械", sub_category="无刷电机驱动板", status="急急待修", status_color="error", location="南区智能实验室", stock=1, min_stock=1)
            db.session.add_all([item1, item2, item3, item4])
            db.session.commit() 
            
            # 种子历史初始化数据完美契合多件与单件模式
            record1 = BorrowRecord(user_id=user_id_badge, item_id=item1.id, return_date=datetime.now()+timedelta(days=7), reason="实验室调试设备", status='等待审批', borrow_date=datetime.now())
            db.session.add(record1); db.session.flush()
            db.session.add(BorrowDetail(record_id=record1.id, item_id=item1.id, quantity=1))
            
            record2 = BorrowRecord(user_id=user_id_badge, item_id=item2.id, return_date=datetime.now()+timedelta(days=5), reason="日常外设补充", status='进行中', borrow_date=datetime.now() - timedelta(days=3))
            db.session.add(record2); db.session.flush()
            db.session.add(BorrowDetail(record_id=record2.id, item_id=item2.id, quantity=2))
            
            db.session.commit()
            print("🎉 活数据库存闭环流水线全部组装成功！")

if __name__ == '__main__':
    # 1. 确保在服务腾飞前，底层的数据库结构和种子数据已安全落库
    init_db()
    
    app.run(host='0.0.0.0', port=5000, debug=False)