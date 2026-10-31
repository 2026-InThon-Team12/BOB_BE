import logging
import time
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.db import create_session_factory

logger = logging.getLogger("bob.api")


def create_app(settings: Settings | None = None, session_factory=None) -> FastAPI:
    settings = settings or get_settings()
    factory = session_factory or create_session_factory(settings.database_url)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        yield
        if session_factory is None:
            factory.kw["bind"].dispose()

    application = FastAPI(title="BOB API", version="0.1.0", lifespan=lifespan)
    application.state.session_factory = factory
    application.dependency_overrides[get_settings] = lambda: settings
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        expose_headers=["X-Request-ID"],
    )

    @application.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = str(uuid4())
        start = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        # Only status/timing; request bodies, tokens and screenshot content are never logged.
        logger.info(
            "request id=%s method=%s status=%d duration_ms=%d",
            request.state.request_id,
            request.method,
            response.status_code,
            int((time.monotonic() - start) * 1000),
        )
        return response

    @application.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {"code": exc.code, "message": exc.message, "request_id": request.state.request_id}
            },
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Do not echo input values (they may contain device tokens).
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "입력값을 확인해 주세요.",
                    "fields": [{"loc": list(error["loc"]), "type": error["type"]} for error in exc.errors()],
                    "request_id": request.state.request_id,
                }
            },
        )

    @application.get("/health", tags=["health"])
    def health():
        return {"status": "ok"}

    @application.get("/health/ready", tags=["health"])
    def ready():
        try:
            with factory() as session:
                session.execute(text("SELECT 1"))
            return {"status": "ok", "database": "ok"}
        except Exception:
            return JSONResponse(status_code=503, content={"status": "unavailable", "database": "unavailable"})

    from app.api.ai import router as ai_router
    from app.api.core import router as core_router
    from app.api.finance import router as finance_router
    from app.api.experience import router as experience_router
    from app.api.inputs import router as inputs_router
    from app.api.post_purchase import router as post_purchase_router
    from app.api.pre_purchase import router as pre_purchase_router
    from app.api.local_questions import router as local_questions_router
    from app.api.goal_shelf import router as goal_shelf_router
    from app.api.goal_setup import router as goal_setup_router

    for router in (
        core_router,
        ai_router,
        finance_router,
        experience_router,
        inputs_router,
        post_purchase_router,
        pre_purchase_router,
        local_questions_router,
        goal_shelf_router,
        goal_setup_router,
    ):
        application.include_router(router, prefix="/api/v1")
    return application


app = create_app()
