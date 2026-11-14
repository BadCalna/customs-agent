"""Prepare per-checkout local state during make setup."""
import os
import secrets
from pathlib import Path


def main():
    local_dir = Path(__file__).resolve().parents[1] / ".local"
    local_dir.mkdir(mode=0o700, exist_ok=True)
    try:
        fd = os.open(local_dir / "dev-secret", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "w") as secret_file:
        secret_file.write(secrets.token_urlsafe(64))


if __name__ == "__main__":
    main()
