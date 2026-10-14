import base64
import hashlib
import hmac
import secrets
import sys
from getpass import getpass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import Admin

basic_auth = HTTPBasic()
ITERATIONS = 600_000


def hash_password(password: str, pepper: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", (password + pepper).encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"


def verify_password(password: str, encoded: str, pepper: str) -> bool:
    try:
        algorithm, rounds, salt_text, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode())
        actual = hashlib.pbkdf2_hmac("sha256", (password + pepper).encode(), salt, int(rounds))
        return hmac.compare_digest(base64.urlsafe_b64encode(actual).decode(), expected)
    except (ValueError, TypeError):
        return False


async def require_admin(credentials: HTTPBasicCredentials = Depends(basic_auth)) -> str:
    settings = get_settings()
    valid_user = hmac.compare_digest(credentials.username, settings.admin_username)
    valid_password = verify_password(
        credentials.password,
        settings.admin_password_hash,
        settings.admin_secret.get_secret_value(),
    )
    if valid_user and valid_password:
        return credentials.username
    if settings.admin_secret.get_secret_value():
        async with SessionLocal() as session:
            admin = await session.scalar(select(Admin).where(Admin.username == credentials.username, Admin.is_active.is_(True)))
            if admin and verify_password(credentials.password, admin.password_hash, settings.admin_secret.get_secret_value()):
                from datetime import UTC, datetime
                admin.last_login_at = datetime.now(UTC)
                await session.commit()
                return admin.username
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid admin credentials",
        headers={"WWW-Authenticate": "Basic"},
    )


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "hash":
    secret = getpass("ADMIN_SECRET pepper: ")
    print(hash_password(getpass("Admin password: "), secret))
