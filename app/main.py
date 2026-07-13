"""FastAPI application entry point."""
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.routers import (
    categories,
    comments,
    favorites,
    market,
    market_favorites,
    reviews,
    skills,
    upload,
    users,
)

settings = get_settings()

app = FastAPI(title=settings.app_name)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(IntegrityError)
async def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    """Turn DB constraint violations into a clean 400 rather than a 500."""
    return JSONResponse(
        status_code=400,
        content={"detail": "Database constraint violation", "error": str(exc.orig)},
    )


app.include_router(users.router)
app.include_router(favorites.router)
app.include_router(market_favorites.router)
app.include_router(skills.router)
app.include_router(upload.router)
app.include_router(reviews.router)
app.include_router(comments.router)
app.include_router(categories.router)
app.include_router(market.router)


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}
