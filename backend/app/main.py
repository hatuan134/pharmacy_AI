from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from collections import defaultdict, deque
from decimal import Decimal
from typing import Literal
from pathlib import Path
from urllib.parse import urlparse
import os
import json
import time as clock
from threading import Lock

from fastapi import FastAPI, Depends, HTTPException, Request, Response, Query, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError

from .config import settings, today
from .db import get_db, Base, engine
from .models import *
from .schemas import *
from .auth import current_user, staff, ai_user, manager, roles, verify_password, hash_password, token
from .services import require, row, movement, batch_rows, alerts, create_sale, cancel_sale, invoice_detail, audit
from . import ai


def documented_header(x_requested_with: str = Header(default='pharmacy')):
    return x_requested_with


app = FastAPI(title='An Tâm · Quản lý nhà thuốc', version='2.0.0', dependencies=[Depends(documented_header)])
origins = [x.strip() for x in settings.cors_origins.split(',')]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'],
    allow_headers=['Content-Type', 'X-Requested-With'],
)



def _same_origin(request: Request, origin: str) -> bool:
    try:
        parsed = urlparse(origin)
        return parsed.netloc.lower() == (request.headers.get('host') or '').lower()
    except ValueError:
        return False


@app.middleware('http')
async def csrf(request: Request, call_next):
    if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
        origin = request.headers.get('origin')
        origin_allowed = (not origin) or origin in origins or _same_origin(request, origin)
        if not origin_allowed or request.headers.get('x-requested-with') != 'pharmacy':
            return JSONResponse(status_code=403, content={'detail': 'Yêu cầu không hợp lệ. Vui lòng thao tác từ ứng dụng.'})
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.exception_handler(IntegrityError)
async def integrity_error(request, exc):
    return JSONResponse(status_code=409, content={'detail': 'Dữ liệu trùng hoặc đang được sử dụng. Kiểm tra mã, tên và các liên kết.'})


@app.get('/api/health')
def health(db=Depends(get_db)):
    db.execute(select(1))
    return {'status': 'ok', 'version': '2.0.0'}


_attempts = defaultdict(deque)
_attempt_lock = Lock()


def rate_limit(key, limit, seconds):
    with _attempt_lock:
        now = clock.monotonic()
        if len(_attempts) > 5000:
            for old in list(_attempts):
                if not _attempts[old] or _attempts[old][-1] < now - 3600:
                    del _attempts[old]
        queue = _attempts[key]
        while queue and queue[0] < now - seconds:
            queue.popleft()
        if len(queue) >= limit:
            raise HTTPException(429, 'Bạn thao tác quá nhanh. Vui lòng thử lại sau.')
        queue.append(now)


# ---------------- Authentication & staff ----------------
@app.post('/api/auth/login')
def login(data: Login, response: Response, request: Request, db=Depends(get_db)):
    rate_limit('login:' + (request.client.host if request.client else 'local'), 15, 300)
    user = db.scalar(select(User).where(User.username == data.username))
    dummy = '0' * 32 + ':' + '0' * 64
    valid = verify_password(data.password, user.password_hash if user else dummy)
    if not user or not valid or not user.active:
        raise HTTPException(401, 'Tên đăng nhập hoặc mật khẩu không đúng.')
    response.set_cookie('session', token(user), httponly=True, samesite='strict', secure=settings.cookie_secure, max_age=28800)
    audit(db, user, 'auth.login', 'user', user.id, {'role': user.role})
    db.commit()
    return row(user)


@app.get('/api/auth/me')
def me(user=Depends(current_user)):
    return row(user)


@app.post('/api/auth/logout')
def logout(response: Response, user=Depends(current_user), db=Depends(get_db)):
    audit(db, user, 'auth.logout', 'user', user.id)
    user.token_version += 1
    db.commit()
    response.delete_cookie('session')
    return {'message': 'Đã đăng xuất.'}


@app.post('/api/auth/password')
def password(data: Password, response: Response, user=Depends(current_user), db=Depends(get_db)):
    if not verify_password(data.old_password, user.password_hash):
        raise HTTPException(400, 'Mật khẩu cũ không đúng.')
    user.password_hash = hash_password(data.new_password)
    user.token_version += 1
    audit(db, user, 'auth.password_change', 'user', user.id)
    db.commit()
    response.set_cookie('session', token(user), httponly=True, samesite='strict', secure=settings.cookie_secure, max_age=28800)
    return {'message': 'Đã đổi mật khẩu.'}


@app.get('/api/users')
def users(user=Depends(manager), db=Depends(get_db)):
    return [row(u) for u in db.scalars(select(User).order_by(User.id))]


@app.post('/api/users')
def user_create(data: UserIn, user=Depends(manager), db=Depends(get_db)):
    item = User(**data.model_dump(exclude={'password'}), password_hash=hash_password(data.password))
    db.add(item)
    db.flush()
    audit(db, user, 'user.create', 'user', item.id, {'username': item.username, 'role': item.role})
    db.commit()
    return row(item)


@app.patch('/api/users/{ident}')
def user_state(ident: int, data: UserState, user=Depends(manager), db=Depends(get_db)):
    item = require(db, User, ident)
    if item.id == user.id:
        raise HTTPException(409, 'Không thể khóa tài khoản đang đăng nhập.')
    before = item.active
    item.active = data.active
    item.token_version += 1
    audit(db, user, 'user.state', 'user', item.id, {'from': before, 'to': data.active})
    db.commit()
    return row(item)


# ---------------- Catalog with real role separation ----------------
def catalog(path, model, schema, create_permission=staff, update_permission=staff, delete_permission=manager, read_permission=current_user):
    def listing(user=Depends(read_permission), db=Depends(get_db)):
        return [row(x) for x in db.scalars(select(model).order_by(model.id.desc()))]

    def validate(db, values, existing=None):
        if model is Medicine:
            require(db, Category, values['category_id'])
            require(db, Unit, values['unit_id'])
            if existing and existing.unit_id != values['unit_id'] and db.scalar(select(Batch.id).where(Batch.medicine_id == existing.id).limit(1)):
                raise HTTPException(409, 'Thuốc đã có lô: không thể đổi đơn vị tính. Hãy tạo mã thuốc mới.')

    def create(data: schema, user=Depends(create_permission), db=Depends(get_db)):
        values = data.model_dump()
        validate(db, values)
        obj = model(**values)
        db.add(obj)
        db.flush()
        audit(db, user, f'{path}.create', path, obj.id, {'after': row(obj)})
        db.commit()
        return row(obj)

    def update(ident: int, data: schema, user=Depends(update_permission), db=Depends(get_db)):
        obj = require(db, model, ident)
        before = row(obj)
        values = data.model_dump()
        validate(db, values, obj)
        if model in (Medicine, Procedure):
            obj.approved = False
            obj.approved_by = None
        for key, value in values.items():
            setattr(obj, key, value)
        audit(db, user, f'{path}.update', path, obj.id, {'before': before, 'after': row(obj)})
        db.commit()
        return row(obj)

    def delete(ident: int, user=Depends(delete_permission), db=Depends(get_db)):
        obj = require(db, model, ident)
        snapshot = row(obj)
        audit(db, user, f'{path}.delete', path, ident, {'before': snapshot})
        db.delete(obj)
        db.commit()
        return {'message': 'Đã xóa.'}

    app.add_api_route('/api/' + path, listing, methods=['GET'], name=path + '_list')
    app.add_api_route('/api/' + path, create, methods=['POST'], name=path + '_create')
    app.add_api_route('/api/' + path + '/{ident}', update, methods=['PUT'], name=path + '_update')
    app.add_api_route('/api/' + path + '/{ident}', delete, methods=['DELETE'], name=path + '_delete')


# Manager owns system catalogs/suppliers/procedures. Pharmacist can curate medicines.
catalog('categories', Category, NameIn, manager, manager, manager)
catalog('units', Unit, NameIn, manager, manager, manager)
catalog('suppliers', Supplier, SupplierIn, manager, manager, manager, staff)
catalog('medicines', Medicine, MedicineIn, staff, staff, manager)
catalog('procedures', Procedure, ProcedureIn, manager, manager, manager)


@app.post('/api/{kind}/{ident}/approve')
def approve(kind: Literal['medicines', 'procedures'], ident: int, user=Depends(current_user), db=Depends(get_db)):
    if kind == 'procedures' and user.role != 'manager':
        raise HTTPException(403, 'Chỉ quản lý được duyệt quy trình nội bộ.')
    if kind == 'medicines' and user.role not in ('manager', 'pharmacist'):
        raise HTTPException(403, 'Bạn không có quyền duyệt thông tin thuốc.')
    obj = require(db, Medicine if kind == 'medicines' else Procedure, ident)
    if kind == 'medicines' and (not obj.information or not obj.source):
        raise HTTPException(422, 'Cần nhập thông tin thuốc và nguồn trước khi duyệt.')
    obj.approved = True
    obj.approved_by = user.id
    audit(db, user, f'{kind}.approve', kind, obj.id)
    db.commit()
    return row(obj)


# ---------------- Inventory / batches ----------------
@app.get('/api/batches')
def batches(q: str = '', category_id: int | None = None, expiry_before: date | None = None, available: bool = False, user=Depends(current_user), db=Depends(get_db)):
    rows = batch_rows(db, q, category_id, expiry_before, available)
    # Cashier does not need purchase price or supplier purchasing details.
    if user.role == 'cashier':
        for item in rows:
            item.pop('purchase_price', None)
            item.pop('supplier_id', None)
            item.pop('supplier_name', None)
    return rows


@app.post('/api/batches')
def batch_create(data: BatchIn, user=Depends(staff), db=Depends(get_db)):
    medicine = require(db, Medicine, data.medicine_id)
    supplier = require(db, Supplier, data.supplier_id)
    if not medicine.active or not supplier.active:
        raise HTTPException(422, 'Thuốc hoặc nhà cung cấp đã ngừng hoạt động.')
    if data.expiry_date <= today() or data.expiry_date <= data.received_date or data.received_date > today():
        raise HTTPException(422, 'Ngày nhập không được ở tương lai; hạn dùng phải sau ngày nhập và sau hôm nay.')
    batch = Batch(**data.model_dump())
    db.add(batch)
    db.flush()
    movement(db, batch, user, batch.quantity, 'receipt', 'Nhập lô mới')
    audit(db, user, 'batch.create', 'batch', batch.id, {'medicine_id': batch.medicine_id, 'code': batch.code, 'quantity': batch.quantity})
    db.commit()
    return row(batch)


@app.patch('/api/batches/{ident}/price')
def batch_price(ident: int, data: BatchPrice, user=Depends(manager), db=Depends(get_db)):
    batch = db.scalar(select(Batch).where(Batch.id == ident).with_for_update())
    if not batch:
        raise HTTPException(404, 'Không tìm thấy lô.')
    old_price = batch.sale_price
    batch.sale_price = data.sale_price
    movement(db, batch, user, 0, 'price', f'Giá bán: {old_price} → {data.sale_price}')
    audit(db, user, 'batch.price', 'batch', batch.id, {'from': str(old_price), 'to': str(data.sale_price)})
    db.commit()
    return row(batch)


@app.post('/api/batches/{ident}/adjust')
def adjust(ident: int, data: Adjustment, user=Depends(manager), db=Depends(get_db)):
    batch = db.scalar(select(Batch).where(Batch.id == ident).with_for_update())
    if not batch:
        raise HTTPException(404, 'Không tìm thấy lô.')
    if batch.quantity != data.expected_quantity:
        raise HTTPException(409, 'Tồn kho đã thay đổi. Tải lại rồi kiểm kê lại.')
    before = batch.quantity
    delta = data.quantity - batch.quantity
    batch.quantity = data.quantity
    movement(db, batch, user, delta, 'adjustment', data.reason)
    audit(db, user, 'batch.adjust', 'batch', batch.id, {'from': before, 'to': data.quantity, 'reason': data.reason})
    db.commit()
    return row(batch)


@app.get('/api/movements')
def movements(user=Depends(staff), db=Depends(get_db)):
    return [row(x) for x in db.scalars(select(Movement).order_by(Movement.id.desc()).limit(1000))]


# Pharmacist requests sensitive stock/price changes; manager decides.
@app.post('/api/approval-requests')
def approval_create(data: ApprovalRequestIn, user=Depends(roles('pharmacist')), db=Depends(get_db)):
    batch = require(db, Batch, data.batch_id)
    if data.kind == 'stock_adjustment':
        if data.quantity is None or data.expected_quantity is None:
            raise HTTPException(422, 'Yêu cầu kiểm kê cần số lượng thực tế và tồn hiện tại.')
        if batch.quantity != data.expected_quantity:
            raise HTTPException(409, 'Tồn kho đã thay đổi. Tải lại trước khi gửi yêu cầu.')
        payload = {'quantity': data.quantity, 'expected_quantity': data.expected_quantity}
    else:
        if data.sale_price is None:
            raise HTTPException(422, 'Yêu cầu đổi giá cần giá bán mới.')
        payload = {'sale_price': str(data.sale_price)}
    pending = db.scalar(select(ApprovalRequest).where(ApprovalRequest.requester_id == user.id, ApprovalRequest.batch_id == batch.id, ApprovalRequest.kind == data.kind, ApprovalRequest.status == 'pending'))
    if pending:
        raise HTTPException(409, 'Bạn đã có một yêu cầu cùng loại đang chờ duyệt cho lô này.')
    req = ApprovalRequest(requester_id=user.id, kind=data.kind, batch_id=batch.id, payload=json.dumps(payload), reason=data.reason)
    db.add(req)
    db.flush()
    audit(db, user, 'approval.create', 'approval_request', req.id, {'kind': req.kind, 'batch_id': batch.id, 'payload': payload, 'reason': req.reason})
    db.commit()
    return {**row(req), 'requester_name': user.name, 'batch_code': batch.code}


@app.get('/api/approval-requests')
def approval_list(user=Depends(roles('manager', 'pharmacist')), db=Depends(get_db)):
    stmt = select(ApprovalRequest).order_by(ApprovalRequest.id.desc()).limit(500)
    if user.role == 'pharmacist':
        stmt = stmt.where(ApprovalRequest.requester_id == user.id)
    result = []
    for req in db.scalars(stmt):
        batch = require(db, Batch, req.batch_id)
        medicine = require(db, Medicine, batch.medicine_id)
        requester = require(db, User, req.requester_id)
        data = row(req)
        data.update({'payload': json.loads(req.payload or '{}'), 'batch_code': batch.code, 'medicine_name': medicine.name, 'requester_name': requester.name})
        result.append(data)
    return result


@app.post('/api/approval-requests/{ident}/decision')
def approval_decision(ident: int, data: ApprovalDecision, user=Depends(manager), db=Depends(get_db)):
    req = db.scalar(select(ApprovalRequest).where(ApprovalRequest.id == ident).with_for_update())
    if not req:
        raise HTTPException(404, 'Không tìm thấy yêu cầu.')
    if req.status != 'pending':
        raise HTTPException(409, 'Yêu cầu này đã được xử lý.')
    batch = db.scalar(select(Batch).where(Batch.id == req.batch_id).with_for_update())
    payload = json.loads(req.payload or '{}')
    if data.decision == 'approved':
        if req.kind == 'stock_adjustment':
            expected = int(payload['expected_quantity'])
            if batch.quantity != expected:
                raise HTTPException(409, 'Tồn kho đã thay đổi sau khi gửi yêu cầu. Không thể duyệt tự động.')
            new_qty = int(payload['quantity'])
            delta = new_qty - batch.quantity
            batch.quantity = new_qty
            movement(db, batch, user, delta, 'adjustment', f'Duyệt yêu cầu #{req.id}: {req.reason}')
        elif req.kind == 'price_change':
            old = batch.sale_price
            batch.sale_price = Decimal(str(payload['sale_price']))
            movement(db, batch, user, 0, 'price', f'Duyệt yêu cầu #{req.id}: {old} → {batch.sale_price}')
    req.status = data.decision
    req.reviewer_id = user.id
    req.review_note = data.note
    req.reviewed_at = datetime.now(timezone.utc)
    audit(db, user, f'approval.{data.decision}', 'approval_request', req.id, {'kind': req.kind, 'batch_id': req.batch_id, 'note': data.note})
    db.commit()
    return row(req)


# ---------------- Alerts, sales, reports ----------------
@app.get('/api/alerts')
def alert_list(days: int = Query(90, ge=1, le=365), user=Depends(current_user), db=Depends(get_db)):
    return alerts(db, days)


@app.post('/api/invoices')
def sale(data: Sale, user=Depends(current_user), db=Depends(get_db)):
    return create_sale(db, data, user)


@app.get('/api/invoices')
def invoices(user=Depends(current_user), db=Depends(get_db)):
    stmt = select(Invoice).order_by(Invoice.id.desc()).limit(1000)
    return [row(i) for i in db.scalars(stmt)]


@app.get('/api/invoices/{ident}')
def invoice(ident: int, user=Depends(current_user), db=Depends(get_db)):
    item = require(db, Invoice, ident)
    return invoice_detail(db, item)


@app.post('/api/invoices/{ident}/cancel')
def cancel(ident: int, data: Cancel, user=Depends(manager), db=Depends(get_db)):
    return cancel_sale(db, ident, data.reason, user)


def report_payload(db, start: date, end: date):
    if start > end:
        raise HTTPException(422, 'Ngày bắt đầu phải trước ngày kết thúc.')
    zone = ZoneInfo('Asia/Ho_Chi_Minh')
    lower = datetime.combine(start, time.min, zone).astimezone(timezone.utc)
    upper = datetime.combine(end + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
    rows = list(db.scalars(select(Invoice).where(Invoice.status == 'paid', Invoice.created_at >= lower, Invoice.created_at < upper)))
    series = defaultdict(Decimal)
    for inv in rows:
        timestamp = inv.created_at.replace(tzinfo=timezone.utc) if inv.created_at.tzinfo is None else inv.created_at
        series[timestamp.astimezone(zone).date().isoformat()] += inv.total
    stock = batch_rows(db)
    return {
        'start': start,
        'end': end,
        'revenue': sum((i.total for i in rows), Decimal(0)),
        'invoice_count': len(rows),
        'stock_value': sum((b['quantity'] * b['purchase_price'] for b in stock), Decimal(0)),
        'total_units': sum(b['quantity'] for b in stock),
        'medicine_count': len(list(db.scalars(select(Medicine).where(Medicine.active.is_(True))))),
        'series': [{'date': k, 'revenue': v} for k, v in sorted(series.items())],
        'alerts': alerts(db),
    }


@app.get('/api/reports')
def reports(start: date | None = None, end: date | None = None, user=Depends(manager), db=Depends(get_db)):
    return report_payload(db, start or today().replace(day=1), end or today())


@app.get('/api/dashboard')
def dashboard(user=Depends(current_user), db=Depends(get_db)):
    warning = alerts(db)
    if user.role == 'manager':
        data = report_payload(db, today().replace(day=1), today())
        data.update({
            'role': 'manager',
            'staff_count': db.scalar(select(func.count(User.id)).where(User.active.is_(True))) or 0,
            'pending_approvals': db.scalar(select(func.count(ApprovalRequest.id)).where(ApprovalRequest.status == 'pending')) or 0,
        })
        return data
    if user.role == 'pharmacist':
        available = batch_rows(db, available=True)
        return {
            'role': 'pharmacist',
            'alerts': warning,
            'available_units': sum(x['quantity'] for x in available),
            'available_batches': len(available),
            'expired_batches': sum(1 for x in warning['expiry'] if x['days_left'] <= 0),
            'expiring_batches': sum(1 for x in warning['expiry'] if 0 < x['days_left'] <= 90),
            'pending_requests': db.scalar(select(func.count(ApprovalRequest.id)).where(ApprovalRequest.requester_id == user.id, ApprovalRequest.status == 'pending')) or 0,
        }
    zone = ZoneInfo('Asia/Ho_Chi_Minh')
    lower = datetime.combine(today(), time.min, zone).astimezone(timezone.utc)
    upper = datetime.combine(today() + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
    own = list(db.scalars(select(Invoice).where(Invoice.user_id == user.id, Invoice.status == 'paid', Invoice.created_at >= lower, Invoice.created_at < upper)))
    return {
        'role': 'cashier',
        'today_invoice_count': len(own),
        'today_sales': sum((x.total for x in own), Decimal(0)),
        'available_batches': len(batch_rows(db, available=True)),
        'low_stock_count': len(warning['low_stock']),
        'alerts': {'expiry': warning['expiry'][:5], 'low_stock': warning['low_stock'][:5]},
    }


# ---------------- Audit ----------------
@app.get('/api/audit-logs')
def audit_logs(user=Depends(manager), db=Depends(get_db)):
    records = []
    for item in db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(500)):
        data = row(item)
        actor = db.get(User, item.user_id) if item.user_id else None
        data['user_name'] = actor.name if actor else 'Hệ thống'
        data['role'] = actor.role if actor else 'system'
        try:
            data['details'] = json.loads(item.details or '{}')
        except ValueError:
            pass
        records.append(data)
    return records


# ---------------- AI ----------------
# Backward-compatible endpoint kept for old screens/tests.
@app.post('/api/ai/ask')
def ask(data: AIRequest, user=Depends(staff), db=Depends(get_db)):
    rate_limit('ai:' + str(user.id), 10, 60)
    return ai.answer(db, data, user)


@app.post('/api/ai/chat')
def chat(data: AIChatRequest, user=Depends(ai_user), db=Depends(get_db)):
    rate_limit('ai-chat:' + str(user.id), 15, 60)
    return ai.chat(db, data, user)


@app.get('/api/ai/logs')
def ai_logs(user=Depends(manager), db=Depends(get_db)):
    return [row(x) for x in db.scalars(select(AILog).order_by(AILog.id.desc()).limit(200))]


# ---------------- Production frontend ----------------
# In local development Vite serves React on :5173 and proxies /api to FastAPI.
# In Render/Docker, FRONTEND_DIST points to the compiled Vite dist directory so
# the same FastAPI service serves both the UI and API from one HTTPS origin.
_frontend_env = os.getenv('FRONTEND_DIST', '').strip()
_frontend_dist = Path(_frontend_env).resolve() if _frontend_env else None
if _frontend_dist and (_frontend_dist / 'index.html').is_file():
    assets = _frontend_dist / 'assets'
    if assets.is_dir():
        app.mount('/assets', StaticFiles(directory=str(assets)), name='frontend-assets')

    @app.get('/{full_path:path}', include_in_schema=False)
    def frontend_spa(full_path: str):
        # API paths must never fall through to index.html.
        if full_path == 'api' or full_path.startswith('api/'):
            raise HTTPException(404, 'API endpoint không tồn tại.')
        candidate = (_frontend_dist / full_path).resolve()
        try:
            candidate.relative_to(_frontend_dist)
        except ValueError:
            candidate = _frontend_dist / 'index.html'
        if full_path and candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(_frontend_dist / 'index.html'))
