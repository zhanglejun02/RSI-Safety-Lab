"""Explicit local credential loading without shell evaluation or interpolation.

Only caller-approved names are accepted. Blank lines and whole-line comments are
allowed; values are single-line literals, optionally wrapped in matching quotes.
Existing process variables take precedence. No values are returned or logged.
"""
import os
from pathlib import Path
import re
import stat

from rsi_lab.errors import ValidationError
from rsi_lab.infra.artifacts import _checked_path


def load_environment(path, allowed_names) -> tuple[str, ...]:
    """Load allowed assignments from a private regular file; return added names.

    Validate the entire file before changing the process environment. ``export``,
    multiline values, duplicates, interpolation and shell commands are unsupported.
    On POSIX, group/other permission bits must be zero (normally ``chmod 600``).
    """
    names = frozenset(allowed_names)
    if any(type(name) is not str or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
           for name in names):
        raise ValidationError("Invalid environment variable allowlist")
    path = _checked_path(Path(path))
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ValidationError("Environment file must be a regular file")
            if os.name == "posix" and stat.S_IMODE(metadata.st_mode) & 0o077:
                raise ValidationError("Environment file must be private; set its permissions to 600")
            raw = handle.read(65537)
    except OSError:
        raise ValidationError("Cannot read environment file") from None
    if len(raw) > 65536:
        raise ValidationError("Environment file exceeds 64 KiB")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError("Environment file must be UTF-8 text") from None
    pending = {}
    for index, line in enumerate(content.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not separator or name not in names or name in pending:
            raise ValidationError(f"Unsupported or duplicate environment assignment on line {index}")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValidationError(f"Invalid literal environment value on line {index}")
            value = value[1:-1]
        if (not value or any(ord(char) < 32 or ord(char) == 127 for char in value)
                or any(token in value for token in ("$", "`", "\"", "'", ";"))):
            raise ValidationError(f"Invalid literal environment value on line {index}")
        pending[name] = value
    loaded = []
    for name, value in pending.items():
        if name not in os.environ:
            os.environ[name] = value
            loaded.append(name)
    return tuple(loaded)
