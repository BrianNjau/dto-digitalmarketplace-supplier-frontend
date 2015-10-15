import re
from mock import patch
from app import create_app
from werkzeug.http import parse_cookie
from app import data_api_client
from datetime import datetime, timedelta
from dmutils.formats import DATETIME_FORMAT


class BaseApplicationTest(object):
    def setup(self):
        self.app = create_app('test')
        self.client = self.app.test_client()
        self.get_user_patch = None

    def teardown(self):
        self.teardown_login()

    @staticmethod
    def get_cookie_by_name(response, name):
        cookies = response.headers.getlist('Set-Cookie')
        for cookie in cookies:
            if name in parse_cookie(cookie):
                return parse_cookie(cookie)
        return None

    @staticmethod
    def supplier():
        return {
            "suppliers": {
                "id": 12345,
                "name": "Supplier Name",
                'description': 'Supplier Description',
                'dunsNumber': '999999999',
                'companiesHouseId': 'SC009988',
                'contactInformation': [{
                    'id': 1234,
                    'contactName': 'contact name',
                    'phoneNumber': '099887',
                    'email': 'email@email.com',
                    'website': 'http://myweb.com',
                }],
                'clients': ['one client', 'two clients']
            }
        }

    @staticmethod
    def user(id, email_address, supplier_id, supplier_name, name,
             is_token_valid=True, locked=False, active=True, role='buyer'):

        hours_offset = -1 if is_token_valid else 1
        date = datetime.utcnow() + timedelta(hours=hours_offset)
        password_changed_at = date.strftime(DATETIME_FORMAT)

        user = {
            "id": id,
            "emailAddress": email_address,
            "name": name,
            "role": role,
            "locked": locked,
            'active': active,
            'passwordChangedAt': password_changed_at
        }

        if supplier_id:
            supplier = {
                "supplierId": supplier_id,
                "name": supplier_name,
            }
            user['role'] = 'supplier'
            user['supplier'] = supplier
        return {
            "users": user
        }

    @staticmethod
    def strip_all_whitespace(content):
        pattern = re.compile(r'\s+')
        return re.sub(pattern, '', content)

    @staticmethod
    def services():
        return {
            'services': [
                {
                    'id': 'id',
                    'serviceName': 'serviceName',
                    'frameworkName': 'frameworkName',
                    'lot': 'lot',
                    'serviceSummary': 'serviceSummary'
                }
            ]
        }

    @staticmethod
    def framework(status='open', name='G-Cloud 7', slug='g-cloud-7'):
        return {
            'frameworks': {
                'status': status,
                'name': name,
                'slug': slug
            }
        }

    def teardown_login(self):
        if self.get_user_patch is not None:
            self.get_user_patch.stop()

    def login(self):
        with patch('app.main.views.login.data_api_client') as login_api_client:
            login_api_client.authenticate_user.return_value = self.user(
                123, "email@email.com", 1234, 'Supplier Name', 'Name')

            self.get_user_patch = patch.object(
                data_api_client,
                'get_user',
                return_value=self.user(123, "email@email.com", 1234, 'Supplier Name', 'Name')
            )
            self.get_user_patch.start()

            self.client.post("/suppliers/login", data={
                'email_address': 'valid@email.com',
                'password': '1234567890'
            })

            login_api_client.authenticate_user.assert_called_once_with(
                "valid@email.com", "1234567890")
