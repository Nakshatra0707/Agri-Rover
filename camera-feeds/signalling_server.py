"""
Signalling server for dual-camera WebRTC streaming.
Relays SDP offer/answer between the Pi broadcaster and the browser client.
No camera or WebRTC media code lives here — see broadcaster.py.

Usage:
    python signalling_server.py --port 8080

Flow:
    Pi  → POST /register  (SDP offer)
    Browser → GET  /offer
    Browser → POST /answer (SDP answer)
    Pi  → GET  /answer (polls until browser's answer is ready)
"""

import argparse
import logging
import os
from pathlib import Path

from aiohttp import web

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("signalling-server")

CLIENT_HTML = Path(__file__).parent / "client.html"

# ponytail: single-broadcaster state, no auth/session id — add per-session keys if multiple Pis are ever needed
state = {"offer": None, "answer": None}


async def register(request):
    state["offer"] = await request.json()
    state["answer"] = None
    logger.info("Offer registered")
    return web.json_response({"status": "ok"})


async def get_offer(request):
    if state["offer"] is None:
        return web.json_response({"error": "no offer yet"}, status=404)
    return web.json_response(state["offer"])


async def post_answer(request):
    state["answer"] = await request.json()
    logger.info("Answer received")
    return web.json_response({"status": "ok"})


async def get_answer(request):
    if state["answer"] is None:
        return web.json_response({"error": "no answer yet"}, status=404)
    return web.json_response(state["answer"])


async def index(request):
    return web.Response(content_type="text/html", text=CLIENT_HTML.read_text())


def main():
    parser = argparse.ArgumentParser(description="WebRTC signalling server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    args = parser.parse_args()

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/register", register)
    app.router.add_get("/offer", get_offer)
    app.router.add_post("/answer", post_answer)
    app.router.add_get("/answer", get_answer)

    logger.info(f"Signalling server starting → http://{args.host}:{args.port}")
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
