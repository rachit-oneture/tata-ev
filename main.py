"""Tata EV Diagnostics: single entry point.

Run:  python main.py
"""
import base64
import binascii
import hmac
import os

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.routes import analyze, feedback, samples, sessions


def create_app() -> FastAPI:
    api = FastAPI(title="Tata EV Diagnostics", docs_url=None, redoc_url=None, openapi_url=None)
    password = os.environ.get("DEMO_PASSWORD", "")
    if os.environ.get("RENDER") and not password:
        raise RuntimeError("Set DEMO_PASSWORD before starting the public demo.")

    @api.middleware("http")
    async def protect_demo(request, call_next):
        if not password or request.url.path == "/api/health":
            return await call_next(request)
        authorization = request.headers.get("authorization", "")
        try:
            scheme, token = authorization.split(" ", 1)
            decoded = base64.b64decode(token, validate=True)
            username, supplied = decoded.split(b":", 1)
            valid = (scheme.lower() == "basic" and hmac.compare_digest(username, b"demo")
                     and hmac.compare_digest(supplied, password.encode("utf-8")))
        except (ValueError, binascii.Error):
            valid = False
        if not valid:
            return PlainTextResponse("Authentication required", status_code=401,
                                     headers={"WWW-Authenticate": 'Basic realm="Tata EV Demo"'})
        return await call_next(request)

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
