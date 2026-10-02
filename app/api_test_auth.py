"""Shared Basic-auth headers for the isolated API runner and live test client."""
import base64
import os


def get_api_test_headers(headers=None, environ=None):
    environment = os.environ if environ is None else environ
    result = {}
    password = environment.get('ALEXANDRIA_AUTH_PASSWORD', '')
    if password:
        username = environment.get('ALEXANDRIA_AUTH_USERNAME', 'alexandria')
        token = base64.b64encode(f'{username}:{password}'.encode('utf-8')).decode('ascii')
        result['Authorization'] = f'Basic {token}'
    result.update(headers or {})
    return result
