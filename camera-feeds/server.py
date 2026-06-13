"""
Dual-Camera WebRTC Server for Raspberry Pi
==========================================
Requirements:
    pip install aiortc aiohttp opencv-python

Usage:
    python server.py --cam0 /dev/video0 --cam1 /dev/video1 --port 8080

If you only have one camera or want to test with a single device:
    python server.py --cam0 /dev/video0 --cam1 /dev/video0 --port 8080

Camera indices can also be integers:
    python server.py --cam0 0 --cam1 1 --port 8080

EMEET SmartCam C950: EMEET Smar (usb-xhci-hcd.0-1):
	/dev/video2
	/dev/video3
	/dev/media4

EMEET SmartCam C950: EMEET Smar (usb-xhci-hcd.1-1):
	/dev/video0
	/dev/video1
	/dev/media3

"""

import argparse
import asyncio
import json
import logging
import threading
import time

import cv2
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.contrib.media import MediaRelay
from av import VideoFrame

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dual-cam-server")

# ── Camera track ──────────────────────────────────────────────────────────────

class CameraTrack(VideoStreamTrack):
    """
    Reads frames from a V4L2 / USB camera via OpenCV and pushes them
    as WebRTC video frames. Each instance owns its own capture thread.
    """

    kind = "video"

    def __init__(self, device, width=640, height=480, fps=30, label="cam"):
        super().__init__()
        self.label = label
        self._device = device
        self._width = width
        self._height = height
        self._fps = fps

        # Try to open as int index first, then as path string
        try:
            idx = int(device)
            self._cap = cv2.VideoCapture(idx)
        except (ValueError, TypeError):
            self._cap = cv2.VideoCapture(device)

        if not self._cap.isOpened():
            raise RuntimeError(f"Cannot open camera: {device}")

        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self._cap.set(cv2.CAP_PROP_FPS, fps)

        # Pre-read one frame on a background thread to warm up
        self._frame = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        logger.info(f"[{label}] Camera opened: {device}")

    def _read_loop(self):
        while True:
            ret, frame = self._cap.read()
            if ret:
                with self._lock:
                    self._frame = frame
            else:
                time.sleep(0.01)

    async def recv(self):
        pts, time_base = await self.next_timestamp()

        # Grab latest frame (non-blocking)
        with self._lock:
            frame = self._frame

        if frame is None:
            # Return a black frame while camera warms up
            frame = _black_frame(self._width, self._height)

        # BGR → RGB
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        video_frame = VideoFrame.from_ndarray(rgb, format="rgb24")
        video_frame.pts = pts
        video_frame.time_base = time_base
        return video_frame

    def stop(self):
        super().stop()
        self._cap.release()
        logger.info(f"[{self.label}] Camera released")


def _black_frame(w, h):
    import numpy as np
    return np.zeros((h, w, 3), dtype="uint8")


# ── WebRTC signalling ─────────────────────────────────────────────────────────

pcs = set()          # active peer connections
relay = MediaRelay()  # shared relay so multiple viewers share one capture


def build_camera_tracks(args):
    """Create (or re-use) the two camera tracks."""
    cam0 = CameraTrack(args.cam0, args.width, args.height, args.fps, label="cam0")
    cam1 = CameraTrack(args.cam1, args.width, args.height, args.fps, label="cam1")
    return cam0, cam1


# We keep a single pair of tracks for all connections (MediaRelay handles fanout)
_tracks = None


async def offer(request):
    global _tracks

    params = await request.json()
    offer_sdp = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    pc = RTCPeerConnection()
    pcs.add(pc)
    logger.info(f"New peer connection (total: {len(pcs)})")

    @pc.on("connectionstatechange")
    async def on_state():
        logger.info(f"Connection state: {pc.connectionState}")
        if pc.connectionState in ("failed", "closed", "disconnected"):
            await pc.close()
            pcs.discard(pc)

    # Lazily create tracks on first connection
    if _tracks is None:
        _tracks = build_camera_tracks(request.app["args"])

    cam0_track, cam1_track = _tracks

    # Add both video tracks — browser will receive two streams
    pc.addTrack(relay.subscribe(cam0_track))
    pc.addTrack(relay.subscribe(cam1_track))

    await pc.setRemoteDescription(offer_sdp)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return web.Response(
        content_type="application/json",
        text=json.dumps({
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
        }),
    )


async def index(request):
    """Serve the HTML client."""
    with open("client.html", "r") as f:
        return web.Response(content_type="text/html", text=f.read())


async def on_shutdown(app):
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)
    pcs.clear()
    if _tracks:
        for t in _tracks:
            t.stop()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Dual-camera WebRTC server")
    parser.add_argument("--cam0",  default="0",   help="Camera 0 device index or path")
    parser.add_argument("--cam1",  default="1",   help="Camera 1 device index or path")
    parser.add_argument("--width", default=640,  type=int)
    parser.add_argument("--height",default=480,  type=int)
    parser.add_argument("--fps",   default=30,   type=int)
    parser.add_argument("--port",  default=8080, type=int)
    parser.add_argument("--host",  default="0.0.0.0")
    args = parser.parse_args()

    app = web.Application()
    app["args"] = args
    app.on_shutdown.append(on_shutdown)

    app.router.add_get("/",       index)
    app.router.add_post("/offer", offer)

    logger.info(f"Server starting → http://{args.host}:{args.port}")
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
