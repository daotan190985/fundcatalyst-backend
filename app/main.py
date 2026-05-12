"""FastAPI application entry point.

Run locally:
    uvicorn app.main:app --reload

Run in production:
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
import sys

from app.config import settings
from app.database import engine
from app.models.db_models import Base
from app.api import stocks_router, meta_router, news_router, alerts_router, scoring_router
from app.scheduler import start_scheduler


# Configure loguru
logger.remove()
logger.add(sys.stdout, level="DEBUG" if settings.debug else "INFO",
           format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> - <level>{message}</level>")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown hooks."""
    # Startup
    logger.info(f"Starting {settings.app_name} v{settings.app_version}")
    logger.info(f"Database: {settings.database_url.split('@')[-1]}")

    # Create tables if not exist (for dev — use Alembic in production)
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables ensured")

    # Seed default alert rules
    from app.database import SessionLocal
    from app.alerts import AlertEngine
    seed_db = SessionLocal()
    try:
        AlertEngine(seed_db).seed_default_rules()
    except Exception as e:
        logger.warning(f"Could not seed alert rules: {e}")
    finally:
        seed_db.close()

    # Start background jobs
    scheduler = None
    if settings.enable_scheduler:
        scheduler = start_scheduler()

    yield

    # Shutdown
    if scheduler:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="API phân tích cơ bản chứng khoán Việt Nam. Dữ liệu từ vnstock (VCI/TCBS/MSN).",
    lifespan=lifespan,
)

# CORS — restrict origins in production via env var
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Simple request logger."""
    response = await call_next(request)
    logger.info(f"{request.method} {request.url.path} -> {response.status_code}")
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Catch-all: don't leak stack traces in production."""
    logger.exception(f"Unhandled error on {request.url.path}: {exc}")
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "detail": str(exc) if settings.debug else None},
    )


# Routes
app.include_router(meta_router)
app.include_router(stocks_router)
app.include_router(news_router)
app.include_router(alerts_router)
app.include_router(scoring_router)


@app.get("/")
def root():
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs",
        "health": "/health",
    }
