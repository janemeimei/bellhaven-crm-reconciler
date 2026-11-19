from __future__ import annotations

import os
import stat
from pathlib import Path


class CredentialError(ValueError):
    pass


def read_token_file(path: str | Path) -> str:
    token_path = Path(path).expanduser()
    if token_path.is_symlink():
        raise CredentialError("The token file must not be a symbolic link.")
    try:
        descriptor = os.open(token_path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise CredentialError("The private token file could not be opened.") from exc
    with os.fdopen(descriptor, encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise CredentialError("The token path must be a regular file.")
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise CredentialError("The token file must be owned by you and use mode 0600.")
        token = handle.read(4097).strip()
    if not token or len(token) > 4096 or any(char.isspace() for char in token):
        raise CredentialError("The token file must contain one non-empty token.")
    return token
