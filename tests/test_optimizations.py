import csv
import io
import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import inspect


os.environ['APP_ENV'] = 'testing'
os.environ['AUTO_INIT_DB'] = '1'
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
os.environ['QUIET_INIT_DB'] = '1'
os.environ['SECRET_KEY'] = 'test-secret'
os.environ['SEED_DEMO_DATA'] = '1'
os.environ['SESSION_COOKIE_SECURE'] = '0'

from app import (  # noqa: E402
    BorrowDetail,
    BorrowRecord,
    Item,
    User,
    app,
    db,
    init_db,
    user_borrow_quantity,
)


class OptimizationBehaviorTest(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, SEED_DEMO_DATA=True)
        with app.app_context():
            db.drop_all()
            init_db()
        self.client = app.test_client()

    def tearDown(self):
        with app.app_context():
            db.session.remove()

    def csrf_token(self, client=None):
        client = client or self.client
        with client.session_transaction() as session_data:
            return session_data['_csrf_token']

    def login(self, username='admin', password='123456', client=None):
        client = client or self.client
        client.get('/')
        response = client.post(
            '/',
            data={
                'email': username,
                'password': password,
                '_csrf_token': self.csrf_token(client),
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        return response

    def test_csrf_missing_is_rejected_and_session_token_is_accepted(self):
        self.client.get('/')
        token = self.csrf_token()

        rejected = self.client.post(
            '/',
            data={'email': 'admin', 'password': '123456'},
        )
        self.assertEqual(rejected.status_code, 400)

        accepted = self.client.post(
            '/',
            data={
                'email': 'admin',
                'password': '123456',
                '_csrf_token': token,
            },
        )
        self.assertEqual(accepted.status_code, 302)
        with self.client.session_transaction() as session_data:
            self.assertIn('user_id', session_data)

        protected = self.client.post('/api/borrow/submit', json={})
        self.assertEqual(protected.status_code, 400)
        self.assertIn('请求校验失败', protected.get_json()['msg'])

    def test_state_changing_navigation_routes_reject_get(self):
        self.assertEqual(self.client.get('/logout').status_code, 405)
        self.assertEqual(self.client.get('/switch-mode').status_code, 405)

        self.login()
        token = self.csrf_token()
        switched = self.client.post(
            '/switch-mode',
            data={'_csrf_token': token},
        )
        self.assertEqual(switched.status_code, 302)
        with self.client.session_transaction() as session_data:
            self.assertEqual(session_data['view_mode'], 'user')

        logged_out = self.client.post(
            '/logout',
            data={'_csrf_token': token},
        )
        self.assertEqual(logged_out.status_code, 302)
        with self.client.session_transaction() as session_data:
            self.assertNotIn('user_id', session_data)

    def test_security_response_headers(self):
        response = self.client.get('/healthz')
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response.headers['X-Frame-Options'], 'SAMEORIGIN')
        self.assertEqual(
            response.headers['Referrer-Policy'],
            'strict-origin-when-cross-origin',
        )
        self.assertEqual(
            response.headers['Permissions-Policy'],
            'camera=(), microphone=(), geolocation=()',
        )

        self.login()
        authenticated = self.client.get('/inventory')
        self.assertEqual(authenticated.headers['Cache-Control'], 'no-store')

    def test_frequent_borrow_queries_have_indexes(self):
        with app.app_context():
            with db.engine.begin() as connection:
                connection.exec_driver_sql(
                    'DROP INDEX ix_borrow_records_user_status'
                )
            init_db()
            inspector = inspect(db.engine)
            record_indexes = {
                index['name']
                for index in inspector.get_indexes('borrow_records')
            }
            detail_indexes = {
                index['name']
                for index in inspector.get_indexes('borrow_details')
            }

        self.assertIn('ix_borrow_records_user_status', record_indexes)
        self.assertIn('ix_borrow_records_user_borrow_date', record_indexes)
        self.assertIn('ix_borrow_records_status_borrow_date', record_indexes)
        self.assertIn('ix_borrow_records_borrow_date', record_indexes)
        self.assertIn('ix_borrow_details_record_id', detail_indexes)

    def test_borrow_quantity_handles_legacy_and_invalid_details(self):
        with app.app_context():
            user = User(
                username='quantity-user',
                password='unused',
                full_name='Quantity User',
                role='user',
            )
            item = Item.query.first()
            db.session.add(user)
            db.session.flush()

            legacy_record = BorrowRecord(
                user_id=user.id,
                item_id=item.id,
                status='等待审批',
            )
            invalid_record = BorrowRecord(
                user_id=user.id,
                item_id=item.id,
                status='等待审批',
            )
            valid_record = BorrowRecord(
                user_id=user.id,
                item_id=item.id,
                status='等待审批',
            )
            db.session.add_all([legacy_record, invalid_record, valid_record])
            db.session.flush()
            db.session.add_all([
                BorrowDetail(
                    record_id=invalid_record.id,
                    item_id=item.id,
                    quantity=-4,
                ),
                BorrowDetail(
                    record_id=valid_record.id,
                    item_id=item.id,
                    quantity=2,
                ),
            ])
            db.session.commit()

            self.assertEqual(
                user_borrow_quantity(user.id, ['等待审批']),
                3,
            )

    def test_demo_seed_can_be_disabled_for_bootstrap_admin(self):
        with app.app_context():
            db.drop_all()
            app.config['SEED_DEMO_DATA'] = False
            with patch.dict(os.environ, {
                'BOOTSTRAP_ADMIN_USERNAME': 'first-admin',
                'BOOTSTRAP_ADMIN_NAME': 'First Administrator',
                'BOOTSTRAP_ADMIN_PASSWORD': 'strong-bootstrap-password',
            }):
                init_db()

            users = User.query.all()
            self.assertEqual(len(users), 1)
            self.assertEqual(users[0].username, 'first-admin')
            self.assertEqual(users[0].role, 'admin')
            self.assertEqual(Item.query.count(), 0)

    def test_legacy_plaintext_passwords_are_migrated(self):
        with app.app_context():
            user = User.query.filter_by(username='user1').first()
            user.password = 'legacy-password'
            db.session.commit()
            init_db()
            self.assertTrue(user.password.startswith('pbkdf2:'))

        self.login('user1', 'legacy-password')

    def test_production_rejects_insecure_boot_configuration(self):
        project_root = Path(__file__).resolve().parents[1]
        scenarios = [
            ('replace-with-openssl-rand-hex-32', '0', 'SECRET_KEY'),
            ('0123456789abcdef0123456789abcdef', '1', 'SEED_DEMO_DATA'),
        ]
        for secret_key, seed_demo_data, expected_message in scenarios:
            with self.subTest(expected_message=expected_message):
                environment = os.environ.copy()
                environment.update({
                    'APP_ENV': 'production',
                    'AUTO_INIT_DB': '0',
                    'SECRET_KEY': secret_key,
                    'SEED_DEMO_DATA': seed_demo_data,
                })
                result = subprocess.run(
                    [sys.executable, '-c', 'import app'],
                    cwd=project_root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_message, result.stderr)

    def test_csv_deduplicates_upload_and_escapes_export_formulas(self):
        self.login()
        csv_buffer = io.StringIO(newline='')
        writer = csv.writer(csv_buffer)
        writer.writerow([
            '资产名称',
            '资产编号',
            '一级大类',
            '二级小类',
            '存放位置',
            '当前在库数量',
            '实时状态',
        ])
        writer.writerow([
            '=2+3',
            'CSV-DUP-001',
            'IT硬件',
            '服务器',
            '@SUM(A1:A2)',
            '4',
            '库存充足',
        ])
        writer.writerow([
            '重复行不应导入',
            'CSV-DUP-001',
            'IT硬件',
            '服务器',
            '其他位置',
            '9',
            '库存充足',
        ])

        imported = self.client.post(
            '/admin/inventory/import',
            data={
                '_csrf_token': self.csrf_token(),
                'file': (
                    io.BytesIO(csv_buffer.getvalue().encode('utf-8-sig')),
                    'inventory.csv',
                ),
            },
            content_type='multipart/form-data',
        )
        self.assertEqual(imported.status_code, 302)
        with app.app_context():
            matches = Item.query.filter_by(asset_id='CSV-DUP-001').all()
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0].name, '=2+3')
            self.assertEqual(matches[0].stock, 4)

        exported = self.client.get('/admin/inventory/export')
        self.assertEqual(exported.status_code, 200)
        rows = list(csv.reader(io.StringIO(exported.data.decode('utf-8-sig'))))
        exported_row = next(row for row in rows[1:] if row[1] == 'CSV-DUP-001')
        self.assertEqual(exported_row[0], "'=2+3")
        self.assertEqual(exported_row[4], "'@SUM(A1:A2)")

    def test_complete_borrow_state_machine_is_idempotent(self):
        with app.app_context():
            user = User.query.filter_by(username='user1').first()
            item = Item(
                asset_id='STATE-MACHINE-001',
                name='状态机测试物资',
                category='IT硬件',
                sub_category='服务器',
                status='库存充足',
                status_color='emerald',
                location='测试库位',
                stock=5,
                min_stock=1,
            )
            db.session.add(item)
            db.session.flush()
            record = BorrowRecord(
                user_id=user.id,
                item_id=item.id,
                borrow_date=datetime.now(),
                return_date=datetime.now() + timedelta(days=7),
                reason='验证完整审批状态机',
                status='等待审批',
            )
            db.session.add(record)
            db.session.flush()
            db.session.add(BorrowDetail(
                record_id=record.id,
                item_id=item.id,
                quantity=2,
            ))
            db.session.commit()
            record_id = record.id
            item_id = item.id

        self.login('admin')
        admin_token = self.csrf_token()
        approved = self.client.post(
            f'/admin/audit/handle/{record_id}',
            data={'action': '进行中', '_csrf_token': admin_token},
        )
        self.assertEqual(approved.status_code, 302)
        with app.app_context():
            self.assertEqual(db.session.get(BorrowRecord, record_id).status, '进行中')
            self.assertEqual(db.session.get(Item, item_id).stock, 3)

        repeated_approval = self.client.post(
            f'/admin/audit/handle/{record_id}',
            data={'action': '进行中', '_csrf_token': admin_token},
        )
        self.assertEqual(repeated_approval.status_code, 302)
        with app.app_context():
            self.assertEqual(db.session.get(BorrowRecord, record_id).status, '进行中')
            self.assertEqual(db.session.get(Item, item_id).stock, 3)

        user_client = app.test_client()
        self.login('user1', client=user_client)
        requested_return = user_client.post(
            f'/api/borrow/return/{record_id}',
            headers={'X-CSRF-Token': self.csrf_token(user_client)},
        )
        self.assertEqual(requested_return.status_code, 200)
        self.assertTrue(requested_return.get_json()['success'])
        repeated_return = user_client.post(
            f'/api/borrow/return/{record_id}',
            headers={'X-CSRF-Token': self.csrf_token(user_client)},
        )
        self.assertEqual(repeated_return.status_code, 409)
        with app.app_context():
            self.assertEqual(
                db.session.get(BorrowRecord, record_id).status,
                '待归还审核',
            )
            self.assertEqual(db.session.get(Item, item_id).stock, 3)

        confirmed = self.client.post(
            f'/admin/audit/handle/{record_id}',
            data={'action': '已归还', '_csrf_token': admin_token},
        )
        self.assertEqual(confirmed.status_code, 302)
        with app.app_context():
            returned_record = db.session.get(BorrowRecord, record_id)
            self.assertEqual(returned_record.status, '已归还')
            self.assertIsNotNone(returned_record.actual_return_date)
            self.assertEqual(db.session.get(Item, item_id).stock, 5)

        repeated_confirmation = self.client.post(
            f'/admin/audit/handle/{record_id}',
            data={'action': '已归还', '_csrf_token': admin_token},
        )
        self.assertEqual(repeated_confirmation.status_code, 302)
        with app.app_context():
            self.assertEqual(db.session.get(BorrowRecord, record_id).status, '已归还')
            self.assertEqual(db.session.get(Item, item_id).stock, 5)

    def test_revoke_only_deletes_waiting_records(self):
        with app.app_context():
            user = User.query.filter_by(username='user1').first()
            item = Item.query.first()
            waiting_record = BorrowRecord(
                user_id=user.id,
                item_id=item.id,
                status='等待审批',
            )
            active_record = BorrowRecord(
                user_id=user.id,
                item_id=item.id,
                status='进行中',
            )
            db.session.add_all([waiting_record, active_record])
            db.session.flush()
            db.session.add_all([
                BorrowDetail(
                    record_id=waiting_record.id,
                    item_id=item.id,
                    quantity=1,
                ),
                BorrowDetail(
                    record_id=active_record.id,
                    item_id=item.id,
                    quantity=1,
                ),
            ])
            db.session.commit()
            waiting_id = waiting_record.id
            active_id = active_record.id

        self.login('user1')
        headers = {'X-CSRF-Token': self.csrf_token()}
        rejected = self.client.post(
            f'/api/borrow/revoke/{active_id}',
            headers=headers,
        )
        self.assertEqual(rejected.status_code, 409)

        revoked = self.client.post(
            f'/api/borrow/revoke/{waiting_id}',
            headers=headers,
        )
        self.assertEqual(revoked.status_code, 200)
        with app.app_context():
            self.assertIsNone(db.session.get(BorrowRecord, waiting_id))
            self.assertEqual(
                BorrowDetail.query.filter_by(record_id=waiting_id).count(),
                0,
            )
            self.assertIsNotNone(db.session.get(BorrowRecord, active_id))


if __name__ == '__main__':
    unittest.main()
