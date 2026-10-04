from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestBodyLimit:
    """Bound actual streamed bytes, not just the caller-controlled Content-Length header."""

    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        length = headers.get(b"content-length")
        if length is not None:
            try:
                declared_length = int(length)
                if declared_length < 0:
                    raise ValueError
            except ValueError:
                await self.error(scope, receive, send, 400, "invalid_content_length")
                return
            if declared_length > self.max_bytes:
                await self.error(scope, receive, send, 413, "request_too_large")
                return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_bytes:
                await self.error(scope, receive, send, 413, "request_too_large")
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        consumed = False

        async def buffered_receive() -> Message:
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, buffered_receive, send)

    async def error(
        self, scope: Scope, receive: Receive, send: Send, status: int, code: str
    ) -> None:
        response = JSONResponse(
            {"outcome": "rejected", "code": code, "message": "Invalid or oversized request body."},
            status_code=status,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
        await response(scope, receive, send)
