"""Tata EV Diagnostics: single entry point.

Run:  python main.py
"""
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.routes import analyze, feedback, samples, sessions


def create_app() -> FastAPI:
    api = FastAPI(title="Tata EV Diagnostics", docs_url=None, redoc_url=None, openapi_url=None)
    if settings.cors_allowed_origins:
        api.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_allowed_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", "Accept"],
        )

    @api.get("/api/health")
    def health():
        return {"ok": True}

    api.include_router(sessions.router, prefix="/api")
    api.include_router(analyze.router, prefix="/api")
    api.include_router(feedback.router, prefix="/api")
    api.include_router(samples.router, prefix="/api")

    # Frontend: the HTML page at "/" and its CSS/JS under "/static".
    api.mount("/static", StaticFiles(directory=settings.web_dir / "static"), name="static")

    @api.get("/", include_in_schema=False)
    def index():
        return FileResponse(settings.web_dir / "index.html")

    return api


app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host=settings.host, port=settings.port)
