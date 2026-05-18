"""Rate-limiting partagé (slowapi).

Importé par main.py pour installer le middleware et par les routes pour appliquer
des limites par endpoint. Si slowapi n'est pas installé (ancien environnement),
on expose un limiter no-op pour éviter de casser l'app.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


class _NoopLimiter:
    """Limiter de secours : laisse passer tout, log un warning au premier appel."""

    _warned = False

    def limit(self, _spec: str):
        def _decorator(func):
            return func

        return _decorator

    def _warn_once(self) -> None:
        if not self._warned:
            logger.warning(
                "slowapi non installé — rate-limiting désactivé. "
                "Installe slowapi (pip install slowapi) pour réactiver."
            )
            self._warned = True


try:
    from slowapi import Limiter
    from slowapi.errors import RateLimitExceeded
    from slowapi.middleware import SlowAPIMiddleware
    from slowapi.util import get_remote_address

    SLOWAPI_AVAILABLE = True
except Exception:  # pragma: no cover — environnement sans slowapi
    SLOWAPI_AVAILABLE = False
    RateLimitExceeded = Exception  # type: ignore[assignment,misc]
    SlowAPIMiddleware = None  # type: ignore[assignment]

    def get_remote_address(_request):  # type: ignore[no-redef]
        return "anonymous"


def _identifier(request) -> str:
    """Identifie le client : user id si JWT, sinon IP."""
    user = getattr(request.state, "user", None)
    if user is not None:
        try:
            return f"user:{user.id}"
        except Exception:
            pass
    return f"ip:{get_remote_address(request)}"


def _build_limiter():
    if not SLOWAPI_AVAILABLE:
        return _NoopLimiter()
    default = os.getenv("RATE_LIMIT_DEFAULT", "120/minute")
    return Limiter(key_func=_identifier, default_limits=[default], headers_enabled=True)


limiter = _build_limiter()


def install_rate_limiter(app) -> None:
    """Attache le limiter à l'app FastAPI (à appeler depuis main.py)."""
    if not SLOWAPI_AVAILABLE:
        logger.warning(
            "slowapi non installé — endpoints sensibles non protégés contre le brute-force."
        )
        return
    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)

    from fastapi import Request
    from fastapi.responses import JSONResponse

    @app.exception_handler(RateLimitExceeded)
    async def _rl_handler(_request: Request, exc: RateLimitExceeded):
        return JSONResponse(
            status_code=429,
            content={
                "detail": "Trop de requêtes — réessaie dans un instant.",
                "limit": str(exc.detail) if hasattr(exc, "detail") else None,
            },
        )
