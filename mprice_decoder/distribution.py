"""Build-specific upload access. Private copies share one revocable credential."""
import json
from dataclasses import dataclass, field
from pathlib import Path


PRIVATE_ACCESS = Path(__file__).resolve().parent / "assets" / "private-upload.json"
PRIVATE_BUILD = False  # the private packaging workflow compiles this as True


@dataclass(frozen=True)
class PrivateAccess:
    token: str = field(default="", repr=False)
    token_id: int | None = None
    url: str = "https://cozy.markets"
    label: str = "private friends"


def private_access(path=None):
    """None for public builds; unusable private credentials fail closed, never register.

    Only the build tool writes this asset. It is not copied into user settings or
    the local database, so forwarding the exe forwards the same upload access.
    """
    path = Path(path) if path is not None else PRIVATE_ACCESS
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return PrivateAccess() if PRIVATE_BUILD else None
    except (OSError, UnicodeError):
        return PrivateAccess()
    try:
        data = json.loads(raw)
        token, token_id, url = data["token"], data.get("token_id"), data["url"]
        label = data.get("label", "private friends")
        if (not isinstance(token, str) or not token.startswith("cozy_") or len(token) > 100
                or len(token) < 20 or any(c.isspace() for c in token)
                or (token_id is not None and (type(token_id) is not int or token_id < 1))
                or url != "https://cozy.markets" or not isinstance(label, str) or not 1 <= len(label) <= 60):
            return PrivateAccess()
        return PrivateAccess(token=token, token_id=token_id, url=url, label=label)
    except (ValueError, TypeError, KeyError):
        return PrivateAccess()
