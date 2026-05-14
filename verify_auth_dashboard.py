from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

print('=== Auth/dashboard verification ===')

email = 'admin@example.com'
password = 'Password123'

resp = client.post(
    '/api/auth/login',
    json={'email': email, 'password': password}
)
print('login', resp.status_code, resp.text)
if resp.status_code == 401:
    resp = client.post(
        '/api/auth/register',
        json={'email': email, 'password': password}
    )
    print('register', resp.status_code, resp.text)
    if resp.status_code not in (200, 201):
        raise SystemExit(1)
    resp = client.post(
        '/api/auth/login',
        json={'email': email, 'password': password}
    )
    print('login after register', resp.status_code, resp.text)

if resp.status_code != 200:
    raise SystemExit(2)

token = resp.json()['access_token']
headers = {'Authorization': f'Bearer {token}'}
resp = client.get('/api/dashboard/summary', headers=headers)
print('dashboard', resp.status_code, resp.text)
if resp.status_code != 200:
    raise SystemExit(3)

print('SUCCESS: auth and dashboard are working.')
