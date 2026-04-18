"""FastAPI application factory. Mounts routers and exception handlers.

Note: `app/__init__.py` forces `WindowsProactorEventLoopPolicy` on Windows
before this module loads. That's required for Playwright to launch
Chromium — uvicorn's default SelectorEventLoop can't spawn subprocesses.
"""

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.exceptions import (
    ConflictError,
    ExternalServiceError,
    FastRecceError,
    ForbiddenError,
    NotFoundError,
    RateLimitError,
    UnauthorizedError,
    ValidationError,
)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    """Application startup and shutdown hooks."""
    yield


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            origin.strip()
            for origin in get_settings().cors_origins.split(",")
            if origin.strip()
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _register_exception_handlers(app)
    _register_routers(app)

    return app


def _register_exception_handlers(app: FastAPI) -> None:
    """Map domain exceptions to HTTP responses."""

    def _error_response(status: int, code: str, message: str) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content={"errors": [{"code": code, "message": message, "field": None}]},
        )

    @app.exception_handler(NotFoundError)
    async def _not_found(_req: Request, exc: NotFoundError) -> JSONResponse:
        return _error_response(404, "NOT_FOUND", str(exc))

    @app.exception_handler(ConflictError)
    async def _conflict(_req: Request, exc: ConflictError) -> JSONResponse:
        return _error_response(409, "CONFLICT", str(exc))

    @app.exception_handler(ValidationError)
    async def _validation(_req: Request, exc: ValidationError) -> JSONResponse:
        return _error_response(422, "UNPROCESSABLE", str(exc))

    @app.exception_handler(UnauthorizedError)
    async def _unauthorized(_req: Request, exc: UnauthorizedError) -> JSONResponse:
        return _error_response(401, "UNAUTHORIZED", str(exc))

    @app.exception_handler(ForbiddenError)
    async def _forbidden(_req: Request, exc: ForbiddenError) -> JSONResponse:
        return _error_response(403, "FORBIDDEN", str(exc))

    @app.exception_handler(RateLimitError)
    async def _rate_limit(_req: Request, exc: RateLimitError) -> JSONResponse:
        return _error_response(429, "RATE_LIMITED", str(exc))

    @app.exception_handler(ExternalServiceError)
    async def _external(_req: Request, exc: ExternalServiceError) -> JSONResponse:
        return _error_response(502, "EXTERNAL_SERVICE_ERROR", str(exc))

    @app.exception_handler(FastRecceError)
    async def _generic(_req: Request, exc: FastRecceError) -> JSONResponse:
        return _error_response(500, "INTERNAL_ERROR", str(exc))


def _register_routers(app: FastAPI) -> None:
    """Mount API routers under /api/v1. Routes added as modules are built."""
    from app.api import (
        analytics,
        auth,
        outreach,
        properties,
        queries,
        search,
        sources,
    )

    @app.get("/api/v1/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(auth.router)
    app.include_router(sources.router)
    app.include_router(queries.router)
    app.include_router(properties.router)
    app.include_router(outreach.router)
    app.include_router(analytics.router)
    app.include_router(search.router)


app = create_app()
