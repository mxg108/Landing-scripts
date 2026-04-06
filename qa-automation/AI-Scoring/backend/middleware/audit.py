"""ASGI middleware that logs every HTTP request to a JSONL audit file."""

import json
import os
import time
from datetime import datetime, timezone


class AuditLogMiddleware:
    """Wraps the ASGI app to log request method, path, status, and duration."""

    def __init__(self, app):
        self.app = app
        self._log_path = os.getenv("AUDIT_LOG_PATH", "audit.log")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        status_code = None

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        await self.app(scope, receive, send_wrapper)

        # Log after the response is sent
        try:
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "method": scope.get("method", ""),
                "path": scope.get("path", ""),
                "status_code": status_code,
                "duration_ms": duration_ms,
            }
            with open(self._log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass  # Never break a request because of logging
