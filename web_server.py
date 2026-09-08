"""Tiny HTTP server for the booking panel: serves web/index.html, mints a
LiveKit room-join token for the browser mic, and exposes agent.py's live
state as JSON.

Runs as its own process, separate from the LiveKit agent worker (agent.py).
They're linked only through state.STATE_FILE (see state.py) -- a JSON file
on disk. That's a deliberate simplification for this single-active-call demo,
not a general multi-user architecture (see README "Out of scope").
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv
from livekit import api

from state import STATE_FILE

load_dotenv()

ROOM_NAME = os.environ.get("DATAFORGE_ROOM_NAME", "clinic-call")
WEB_DIR = Path(__file__).parent / "web"


async def handle_index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB_DIR / "index.html")


async def handle_state(request: web.Request) -> web.Response:
    if STATE_FILE.exists():
        body = STATE_FILE.read_text(encoding="utf-8")
    else:
        body = json.dumps({"booking": None, "fenced_events": [], "spoken_log": [], "timing_events": []})
    return web.Response(text=body, content_type="application/json")


async def handle_token(request: web.Request) -> web.Response:
    api_key = os.environ.get("LIVEKIT_API_KEY")
    api_secret = os.environ.get("LIVEKIT_API_SECRET")
    livekit_url = os.environ.get("LIVEKIT_URL")
    if not (api_key and api_secret and livekit_url):
        return web.json_response(
            {"error": "LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET not set (see .env.example)"},
            status=500,
        )

    identity = f"caller-{uuid.uuid4().hex[:8]}"
    token = (
        api.AccessToken(api_key, api_secret)
        .with_identity(identity)
        .with_name("Caller")
        .with_grants(api.VideoGrants(room_join=True, room=ROOM_NAME, can_publish=True, can_subscribe=True))
        .to_jwt()
    )
    return web.json_response({"token": token, "url": livekit_url, "room": ROOM_NAME, "identity": identity})


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/state", handle_state)
    app.router.add_get("/api/token", handle_token)
    app.router.add_static("/web/", WEB_DIR)
    return app


if __name__ == "__main__":
    port = int(os.environ.get("DATAFORGE_WEB_PORT", "8080"))
    web.run_app(build_app(), port=port)
