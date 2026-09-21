from app.models import Batch, ApprovalRequest, AuditLog
from app.config import settings


def test_role_separation_and_approval_workflow(setup):
    c, S, ids, tokens = setup
    c.cookies.set('session', tokens['pharmacist'])

    # Pharmacist can work with batches, but sensitive mutation is manager-only.
    assert c.patch(f'/api/batches/{ids["batch"]}/price', json={'sale_price': 2500}).status_code == 403
    assert c.post(
        f'/api/batches/{ids["batch"]}/adjust',
        json={'quantity': 8, 'expected_quantity': 10, 'reason': 'Kiểm kê cuối ca'},
    ).status_code == 403
    assert c.get('/api/reports').status_code == 403

    request = c.post('/api/approval-requests', json={
        'kind': 'stock_adjustment',
        'batch_id': ids['batch'],
        'quantity': 8,
        'expected_quantity': 10,
        'reason': 'Kiểm kê cuối ca lệch hai hộp',
    })
    assert request.status_code == 200, request.text
    request_id = request.json()['id']

    with S() as db:
        assert db.get(Batch, ids['batch']).quantity == 10
        assert db.get(ApprovalRequest, request_id).status == 'pending'

    c.cookies.set('session', tokens['manager'])
    decision = c.post(f'/api/approval-requests/{request_id}/decision', json={'decision': 'approved', 'note': 'Đã đối chiếu biên bản'})
    assert decision.status_code == 200, decision.text

    with S() as db:
        assert db.get(Batch, ids['batch']).quantity == 8
        assert db.get(ApprovalRequest, request_id).status == 'approved'
        assert db.query(AuditLog).count() >= 2


def test_dashboards_and_admin_audit_are_role_scoped(setup):
    c, S, ids, tokens = setup
    for role in ('manager', 'pharmacist', 'cashier'):
        c.cookies.set('session', tokens[role])
        data = c.get('/api/dashboard')
        assert data.status_code == 200
        assert data.json()['role'] == role

    c.cookies.set('session', tokens['cashier'])
    assert c.get('/api/audit-logs').status_code == 403
    assert c.get('/api/approval-requests').status_code == 403

    c.cookies.set('session', tokens['manager'])
    assert c.get('/api/audit-logs').status_code == 200


def test_unified_chatbot_respects_role_without_provider(setup, monkeypatch):
    c, S, ids, tokens = setup
    monkeypatch.setattr(settings, 'gemini_api_key', '')

    c.cookies.set('session', tokens['cashier'])
    denied = c.post('/api/ai/chat', json={'message': 'Doanh thu hôm nay là bao nhiêu?', 'history': []})
    assert denied.status_code == 200
    assert 'không có quyền' in denied.json()['answer'].lower()

    stock = c.post('/api/ai/chat', json={'message': 'Thuốc kiểm thử còn bao nhiêu trong kho?', 'history': []})
    assert stock.status_code == 200, stock.text
    assert 'inventory_search' in stock.json()['used_tools']
    assert any(s['id'].startswith('batch:') for s in stock.json()['sources'])
