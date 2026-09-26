import hashlib
import hmac
import secrets

def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256',password.encode(),salt.encode(),600_000).hex()
    return f'{salt}${digest}'

def verify_password(password, encoded):
    salt, expected = encoded.split('$')
    actual = hashlib.pbkdf2_hmac('sha256',password.encode(),salt.encode(),600_000).hex()
    return hmac.compare_digest(actual,expected)

def token_hash(token):return hashlib.sha256(token.encode()).hexdigest()
