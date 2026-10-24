"""HTTP entry point: the call page, a health check and the call WebSocket."""

from __future__ import annotations

from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pipecat.utils.security.allowed_origins import is_origin_allowed

from .config import Settings, load_settings
from .pipeline import run_call

STATIC_DIR = Path(__file__).parent / "static"
PORT = 8090


def create_app(settings: Settings, *, call=run_call) -> FastAPI:
    app = FastAPI(title="ODS Voice Agent", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/")
    async def page() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.websocket("/ws")
    async def websocket_call(websocket: WebSocket) -> None:
        # Browsers send Origin on every WebSocket; without this check any page
        # the user visits could open a call and use their model gateway key.
        origin = websocket.headers.get("origin", "")
        if not is_origin_allowed(origin, list(settings.allowed_origins)):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        await call(websocket, settings)

    return app


def main() -> None:
    uvicorn.run(create_app(load_settings()), host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
