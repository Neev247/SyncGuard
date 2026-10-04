import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException

from app.api import router
from app.config import Settings
from app.database import Database
from app.errors import Problem
from app.middleware import RequestBodyLimit
from app.service import SyncService

logger = logging.getLogger(__name__)


def safe_diagnostic(value: str) -> str:
    return value.encode("utf-8", errors="backslashreplace").decode("utf-8")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_environment()
    database = Database(settings)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        database.initialize()
        yield

    application = FastAPI(
        title="Offline Sync API",
        version="1.0.0",
        description=(
            "Versioned, field-level offline synchronization. "
            "Create a workspace at **POST /v1/users**, save its token, click **Authorize**, "
            "and register each device. Submit a document UUID, "
            "request UUID, device UUID, base version, and field patch to **POST /v1/sync**. "
            "Conflicts never partially modify a document. "
            "Retries with the same request UUID replay the original result."
        ),
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    application.state.database = database
    application.state.service = SyncService(database)
    application.add_middleware(RequestBodyLimit, max_bytes=settings.max_request_bytes)

    @application.middleware("http")
    async def response_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/v1"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.exception_handler(Problem)
    async def problem_handler(request: Request, error: Problem):
        headers = {"WWW-Authenticate": "Bearer"} if error.status == 401 else {}
        return JSONResponse(
            {"outcome": "rejected", "code": error.code, "message": error.message},
            status_code=error.status,
            headers=headers,
        )

    @application.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, error: RequestValidationError):
        return JSONResponse(
            {
                "outcome": "rejected",
                "code": "validation_error",
                "message": "The request did not pass validation.",
                "errors": [
                    {
                        "location": [
                            safe_diagnostic(part) if isinstance(part, str) else part
                            for part in item["loc"]
                        ],
                        "message": safe_diagnostic(item["msg"]),
                        "type": item["type"],
                    }
                    for item in error.errors()
                ],
            },
            status_code=422,
        )

    @application.exception_handler(HTTPException)
    async def http_handler(request: Request, error: HTTPException):
        return JSONResponse(
            {
                "outcome": "rejected",
                "code": f"http_{error.status_code}",
                "message": str(error.detail),
            },
            status_code=error.status_code,
            headers=error.headers,
        )

    @application.exception_handler(sqlite3.OperationalError)
    async def database_handler(request: Request, error: sqlite3.OperationalError):
        code = getattr(error, "sqlite_errorcode", 0) or 0
        if code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return JSONResponse(
                {"outcome": "rejected", "code": "database_busy", "message": "Retry this request."},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        logger.error("Database operation failed", exc_info=error)
        return JSONResponse(
            {"outcome": "rejected", "code": "storage_error", "message": "Storage is unavailable."},
            status_code=503,
            headers={"Retry-After": "1"},
        )

    @application.exception_handler(Exception)
    async def unexpected_handler(request: Request, error: Exception):
        logger.error("Unexpected request failure", exc_info=error)
        return JSONResponse(
            {
                "outcome": "rejected",
                "code": "internal_error",
                "message": "An internal error occurred.",
            },
            status_code=500,
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/docs")

    @application.get("/healthz", tags=["Operations"])
    def health():
        with database.transaction() as connection:
            connection.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        return {"status": "ok"}

    @application.get("/docs", include_in_schema=False)
    def swagger():
        return get_swagger_ui_html(
            openapi_url="/openapi.json",
            title="Offline Sync API",
            swagger_favicon_url="/favicon.svg",
            swagger_ui_parameters={"persistAuthorization": False, "displayRequestDuration": True},
        )

    @application.get("/redoc", include_in_schema=False)
    def redoc():
        return get_redoc_html(
            openapi_url="/openapi.json", title="Offline Sync API", redoc_favicon_url="/favicon.svg"
        )

    @application.get("/favicon.svg", include_in_schema=False)
    def favicon():
        return FileResponse(
            Path(__file__).parent / "static" / "favicon.svg", media_type="image/svg+xml"
        )

    @application.get("/favicon.ico", include_in_schema=False)
    def favicon_legacy():
        return RedirectResponse("/favicon.svg")

    application.include_router(router)
    return application


app = create_app()
