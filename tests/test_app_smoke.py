import os
import unittest
from datetime import datetime, timedelta


os.environ['AUTO_INIT_DB'] = '1'
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
os.environ['QUIET_INIT_DB'] = '1'
os.environ['SECRET_KEY'] = 'test-secret'

from app import Item, app, db, init_db  # noqa: E402
from wsgi import app as wsgi_app  # noqa: E402


class AppSmokeTest(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        with app.app_context():
            db.drop_all()
            init_db()
        self.client = app.test_client()

    def login(self, username='admin', password='123456'):
        self.client.get('/')
        with self.client.session_transaction() as session_data:
            csrf_token = session_data['_csrf_token']
        return self.client.post(
            '/',
            data={
                'email': username,
                'password': password,
                '_csrf_token': csrf_token,
            },
            follow_redirects=True,
        )

    def csrf_headers(self):
        with self.client.session_transaction() as session_data:
            return {'X-CSRF-Token': session_data['_csrf_token']}

    def test_health_and_wsgi_entrypoint(self):
        response = self.client.get('/healthz')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['status'], 'ok')

        wsgi_client = wsgi_app.test_client()
        self.assertEqual(wsgi_client.get('/healthz').status_code, 200)

    def test_anonymous_requests_are_blocked(self):
        page_response = self.client.get('/catalog')
        self.assertEqual(page_response.status_code, 302)
        self.assertIn('/', page_response.headers['Location'])

        api_response = self.client.post('/api/borrow/submit', json={})
        self.assertEqual(api_response.status_code, 401)
        self.assertFalse(api_response.get_json()['success'])

    def test_admin_pages_render(self):
        response = self.login('admin')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/inventory').status_code, 200)
        self.assertEqual(self.client.get('/audit').status_code, 200)

    def test_user_pages_render_and_admin_pages_are_blocked(self):
        response = self.login('user1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/catalog').status_code, 200)
        self.assertEqual(self.client.get('/borrow').status_code, 200)

        blocked = self.client.get('/inventory')
        self.assertEqual(blocked.status_code, 302)
        self.assertIn('/user/dashboard', blocked.headers['Location'])

    def test_batch_borrow_rejects_aggregated_overstock(self):
        self.login('user1')
        tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')

        with app.app_context():
            item = Item.query.filter_by(asset_id='AST-2026-009').first()
            item_id = str(item.id)

        response = self.client.post(
            '/api/borrow/submit',
            json={
                'reason': '测试重复物资数量合并',
                'return_date': tomorrow,
                'items': [
                    {'item_id': item_id, 'quantity': 2},
                    {'item_id': item_id, 'quantity': 1},
                ],
            },
            headers=self.csrf_headers(),
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()['success'])

    def test_batch_borrow_accepts_valid_request(self):
        self.login('user1')
        tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')

        with app.app_context():
            item = Item.query.filter_by(asset_id='AST-2026-009').first()
            item_id = str(item.id)

        response = self.client.post(
            '/api/borrow/submit',
            json={
                'reason': '测试合法申领',
                'return_date': tomorrow,
                'items': [{'item_id': item_id, 'quantity': 1}],
            },
            headers=self.csrf_headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['success'])


if __name__ == '__main__':
    unittest.main()
