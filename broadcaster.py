"""
Dual-Camera WebRTC Broadcaster for Raspberry Pi
================================================
Requirements:
    pip install aiortc aiohttp opencv-python python-dotenv

Usage:
    python broadcaster.py --cam0 /dev/video0 --cam1 /dev/video1 \\
        --signalling-url https://your-app.onrender.com

TURN credentials are read from a .env file (see .env.example) via
TURN_URL / TURN_USERNAME / TURN_CREDENTIAL.
"""

import argparse
import asyncio
import json
import logging
import os
import threading
import time

import cv2
from aiohttp import ClientSession
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
    VideoStreamTrack,
)
from aiortc.contrib.media import MediaRelay
from av import VideoFrame
from dotenv import load_dotenv
from motor_driver import handle_command
from sensor_reader import SENSOR_LOG_PATH, append_reading, read_log, read_sensors

SENSOR_INTERVAL = 2  # seconds between soil readings

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("broadcaster")

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

        with self._lock:
            frame = self._frame

        if frame is None:
            frame = _black_frame(self._width, self._height)

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


# ── WebRTC ─────────────────────────────────────────────────────────────────

relay = MediaRelay()


async def fetch_turn_configuration(session):
    domain = os.environ["METERED_DOMAIN"]
    api_key = os.environ["METERED_API_KEY"]
    url = f"https://{domain}/api/v1/turn/credentials?apiKey={api_key}"
    async with session.get(url) as resp:
        ice_servers = await resp.json()

    return RTCConfiguration(iceServers=[
        RTCIceServer(
            urls=server["urls"],
            username=server.get("username"),
            credential=server.get("credential"),
        )
        for server in ice_servers
    ])


async def wait_ice_gathering_complete(pc):
    if pc.iceGatheringState == "complete":
        return
    done = asyncio.Event()

    @pc.on("icegatheringstatechange")
    def _on_change():
        if pc.iceGatheringState == "complete":
            done.set()

    await done.wait()


async def run(args):
    async with ClientSession() as session:
        pc = RTCPeerConnection(configuration=await fetch_turn_configuration(session))

        @pc.on("connectionstatechange")
        async def on_state():
            logger.info(f"Connection state: {pc.connectionState}")

        control_channel = pc.createDataChannel("control")

        @control_channel.on("message")
        def on_control_message(message):
            try:
                payload = json.loads(message)
                handle_command(payload["cmd"], payload["state"])
            except (ValueError, KeyError) as e:
                logger.warning(f"Bad control message {message!r}: {e}")

        sensors_channel = pc.createDataChannel("sensors")

        @sensors_channel.on("open")
        def on_sensors_open():
            # Fresh (or reconnected) dashboard — replay everything logged so
            # far so it doesn't miss readings taken while disconnected.
            for entry in read_log(SENSOR_LOG_PATH):
                sensors_channel.send(json.dumps(entry))

        async def sensor_loop():
            while True:
                reading = read_sensors()
                if reading:
                    entry = append_reading(SENSOR_LOG_PATH, reading)
                    if sensors_channel.readyState == "open":
                        sensors_channel.send(json.dumps(entry))
                await asyncio.sleep(SENSOR_INTERVAL)

        cam0 = CameraTrack(args.cam0, args.width, args.height, args.fps, label="cam0")
        cam1 = CameraTrack(args.cam1, args.width, args.height, args.fps, label="cam1")
        pc.addTrack(relay.subscribe(cam0))
        pc.addTrack(relay.subscribe(cam1))

        sensor_task = asyncio.create_task(sensor_loop())

        try:
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            await wait_ice_gathering_complete(pc)

            await session.post(
                f"{args.signalling_url}/register",
                json={"sdp": pc.localDescription.sdp, "type": pc.localDescription.type},
            )
            logger.info("Offer registered, waiting for browser answer…")

            answer = None
            while answer is None:
                async with session.get(f"{args.signalling_url}/answer") as resp:
                    if resp.status == 200:
                        answer = await resp.json()
                    else:
                        await asyncio.sleep(1)

            await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
            logger.info("Answer applied — streaming peer-to-peer")

            while True:
                await asyncio.sleep(3600)
        finally:
            sensor_task.cancel()
            await pc.close()
            cam0.stop()
            cam1.stop()


def main():
    parser = argparse.ArgumentParser(description="Dual-camera WebRTC broadcaster (Pi)")
    parser.add_argument("--cam0", default="0", help="Camera 0 device index or path")
    parser.add_argument("--cam1", default="1", help="Camera 1 device index or path")
    parser.add_argument(
        "--signalling-url",
        default=os.environ.get("SIGNALLING_URL", "http://localhost:8080"),
        help="Base URL of the signalling server",
    )
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--fps", type=int, default=30)
    args = parser.parse_args()

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()