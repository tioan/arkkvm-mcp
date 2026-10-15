"""Async client for ArkKVM devices (WebRTC + JSON-RPC over the "rpc" data channel).

Protocol (derived from arkkvm/arkkvm-app):
  * Signaling: WebSocket ``/webrtc/signaling/client``. Client sends
    ``{"type": "offer", "data": {"sd": base64(json(RTCSessionDescription))}}``;
    device answers ``{"type": "answer", "data": base64(...)}`` and trickles
    ``{"type": "new-ice-candidate", "data": {...}}``. Queued candidates are only
    flushed after the device receives a WS message, so we ping continuously.
    Closing the WebSocket tears the WebRTC session down.
  * Control: JSON-RPC 2.0 on data channel ``rpc``. Events arrive as
    notifications (``videoInputState``, ``usbState``, ``atxState`` ...).
  * Only one session is "current": a new session takes over and the previous
    one is closed by the device about one second later.
"""

from __future__ import annotations

import asyncio
import base64
import io
import itertools
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx
import websockets
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from aiortc.sdp import candidate_from_sdp

log = logging.getLogger("arkkvm_mcp")

ABS_MAX = 32767


try:  # surface messages to the model instead of a masked "Error executing tool"
    from mcp.server.mcpserver.exceptions import ToolError as _ErrBase
except ImportError:
    try:
        from mcp.server.fastmcp.exceptions import ToolError as _ErrBase
    except ImportError:
        _ErrBase = RuntimeError


class ArkKVMError(_ErrBase):
    pass


class Frame:
    """A decoded video frame; converted to a PIL image only when needed."""

    __slots__ = ("_raw", "_image", "seq", "ts")

    def __init__(self, raw: Any, seq: int, ts: float) -> None:
        self._raw, self._image, self.seq, self.ts = raw, None, seq, ts

    @property
    def image(self):  # noqa: ANN201
        if self._image is None:
            self._image = self._raw.to_image()
            self._raw = None
        return self._image


def _thumb(img):  # noqa: ANN001, ANN202
    return img.convert("L").resize((320, 180))


def _mean_diff(a, b) -> float:  # noqa: ANN001
    from PIL import ImageChops, ImageStat

    return ImageStat.Stat(ImageChops.difference(a, b)).mean[0]


@dataclass
class _Conn:
    pc: RTCPeerConnection
    ws: Any
    dc: Any
    tasks: list[asyncio.Task] = field(default_factory=list)


def _base_urls(host: str) -> tuple[str, str]:
    if "://" not in host:
        host = "http://" + host
    u = urlparse(host)
    scheme = u.scheme or "http"
    netloc = u.netloc or u.path
    ws_scheme = "wss" if scheme == "https" else "ws"
    return f"{scheme}://{netloc}", f"{ws_scheme}://{netloc}"


class ArkKVM:
    def __init__(
        self,
        host: str,
        password: str | None = None,
        idle_timeout: float = 120.0,
        verify_tls: bool = False,
    ) -> None:
        self.http_base, self.ws_base = _base_urls(host)
        self.password = password or None
        self.idle_timeout = idle_timeout
        self.verify_tls = verify_tls

        self._conn: _Conn | None = None
        self._lock = asyncio.Lock()
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._frame: Frame | None = None
        self._frame_seq = 0
        self._frame_event = asyncio.Event()
        self._last_used = time.monotonic()
        self._idle_task: asyncio.Task | None = None
        self.events: dict[str, Any] = {}
        self._cookies: dict[str, str] = {}
        self._csrf: str | None = None

    # ------------------------------------------------------------------ auth
    async def _login(self) -> tuple[dict[str, str], str | None]:
        """Return (cookies, csrf_token). Empty when the device runs in noPassword mode.

        GET /device is itself protected when a password is set, so it doubles as the
        auth-mode probe: 200 means no password is required, 401 means log in.
        """
        async with httpx.AsyncClient(base_url=self.http_base, verify=self.verify_tls, timeout=10) as c:
            try:
                probe = await c.get("/device")
            except httpx.HTTPError as e:
                raise ArkKVMError(f"Cannot reach ArkKVM at {self.http_base}: {e}") from None
            if probe.status_code == 200 and probe.json().get("authMode", "noPassword") in ("", "noPassword"):
                return {}, None
            if not self.password:
                raise ArkKVMError("ArkKVM requires a password: set ARKKVM_PASSWORD.")
            r = await c.post("/auth/login-local", json={"password": self.password})
            if r.status_code == 401:
                raise ArkKVMError("ArkKVM login failed: wrong password (check ARKKVM_PASSWORD).")
            if r.status_code != 200:
                raise ArkKVMError(f"ArkKVM login failed: HTTP {r.status_code} {r.text[:200]}")
            csrf = r.headers.get("X-CSRF-Token") or r.json().get("csrfToken")
            cookies = dict(r.cookies)
            if "authToken" not in cookies:
                raise ArkKVMError("ArkKVM login returned no authToken cookie.")
            return cookies, csrf

    # ------------------------------------------------------------ connection
    @property
    def connected(self) -> bool:
        c = self._conn
        return bool(
            c
            and c.dc.readyState == "open"
            and c.pc.connectionState not in ("failed", "closed")
        )

    async def ensure_connected(self) -> None:
        self._last_used = time.monotonic()
        if self.connected:
            return
        async with self._lock:
            if self.connected:
                return
            await self._close_conn()
            try:
                await self._connect()
            except ArkKVMError:
                raise
            except Exception as e:  # noqa: BLE001
                await self._close_conn()
                raise ArkKVMError(f"Connecting to ArkKVM failed: {type(e).__name__}: {e}") from e
            if self._idle_task is None or self._idle_task.done():
                self._idle_task = asyncio.create_task(self._idle_watch())

    async def _connect(self, timeout: float = 15.0) -> None:
        cookies, csrf = await self._login()
        self._cookies, self._csrf = cookies, csrf

        pc = RTCPeerConnection()
        pc.addTransceiver("video", direction="recvonly")
        dc = pc.createDataChannel("rpc", ordered=True)
        opened = asyncio.Event()
        dc.on("open", opened.set)
        dc.on("message", self._on_message)

        conn_tasks: list[asyncio.Task] = []

        @pc.on("track")
        def _on_track(track):  # noqa: ANN001
            if track.kind == "video":
                conn_tasks.append(asyncio.create_task(self._read_video(track)))

        await pc.setLocalDescription(await pc.createOffer())
        sd = base64.b64encode(
            json.dumps({"type": "offer", "sdp": pc.localDescription.sdp}).encode()
        ).decode()

        url = self.ws_base + "/webrtc/signaling/client"
        if csrf:
            url += f"?csrf-token={csrf}"
        headers = {}
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        ssl_ctx = None
        if url.startswith("wss://") and not self.verify_tls:
            import ssl

            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
        ws = await websockets.connect(
            url, additional_headers=headers, ssl=ssl_ctx, open_timeout=10, ping_interval=20
        )
        await ws.send(json.dumps({"type": "offer", "data": {"sd": sd}}))

        conn_tasks.append(asyncio.create_task(self._signal_reader(ws, pc)))
        conn_tasks.append(asyncio.create_task(self._signal_pinger(ws)))
        self._conn = _Conn(pc=pc, ws=ws, dc=dc, tasks=conn_tasks)

        try:
            await asyncio.wait_for(opened.wait(), timeout)
        except asyncio.TimeoutError:
            state = pc.iceConnectionState
            await self._close_conn()
            raise ArkKVMError(
                f"WebRTC connection to {self.http_base} timed out (ICE state: {state}). "
                "UDP between this machine and the KVM must be reachable."
            ) from None
        log.info("connected to %s", self.http_base)

    async def _signal_reader(self, ws, pc: RTCPeerConnection) -> None:  # noqa: ANN001
        try:
            async for raw in ws:
                if raw == "pong":
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                t = msg.get("type")
                if t == "answer":
                    ans = json.loads(base64.b64decode(msg["data"]))
                    await pc.setRemoteDescription(RTCSessionDescription(ans["sdp"], ans["type"]))
                elif t == "new-ice-candidate":
                    c = msg.get("data") or {}
                    cand = c.get("candidate") or ""
                    if not cand:
                        continue
                    ic = candidate_from_sdp(cand.split(":", 1)[1])
                    ic.sdpMid = c.get("sdpMid")
                    ic.sdpMLineIndex = c.get("sdpMLineIndex")
                    try:
                        await pc.addIceCandidate(ic)
                    except Exception as e:  # noqa: BLE001
                        log.debug("ignoring candidate %s: %s", cand, e)
                elif t == "device-metadata":
                    self.events["deviceMetadata"] = msg.get("data")
        except websockets.ConnectionClosed:
            pass

    async def _signal_pinger(self, ws) -> None:  # noqa: ANN001
        # The device only flushes queued ICE candidates after receiving a message.
        delay = 0.2
        try:
            while True:
                await asyncio.sleep(delay)
                await ws.send("ping")
                delay = min(delay * 1.5, 5.0)
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            pass

    async def _read_video(self, track) -> None:  # noqa: ANN001
        try:
            while True:
                frame = await track.recv()
                self._frame_seq += 1
                self._frame = Frame(frame, self._frame_seq, time.monotonic())
                self._frame_event.set()
        except (MediaStreamError, asyncio.CancelledError):
            pass

    def _on_message(self, raw) -> None:  # noqa: ANN001
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        mid = msg.get("id")
        if mid is not None and mid in self._pending:
            fut = self._pending.pop(mid)
            if not fut.done():
                fut.set_result(msg)
        elif "method" in msg:
            self.events[msg["method"]] = msg.get("params")

    async def _idle_watch(self) -> None:
        while True:
            await asyncio.sleep(5)
            if self._conn and self.idle_timeout > 0 and time.monotonic() - self._last_used > self.idle_timeout:
                log.info("idle timeout, releasing KVM session")
                async with self._lock:
                    await self._close_conn()

    async def _close_conn(self) -> None:
        c, self._conn = self._conn, None
        self._frame = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ArkKVMError("connection closed"))
        self._pending.clear()
        if not c:
            return
        for t in c.tasks:
            t.cancel()
        try:
            await c.ws.send(json.dumps({"type": "client-closing"}))
        except Exception:  # noqa: BLE001
            pass
        for closer in (c.pc.close, c.ws.close):
            try:
                await asyncio.wait_for(closer(), 3)
            except Exception:  # noqa: BLE001
                pass

    async def close(self) -> None:
        if self._idle_task:
            self._idle_task.cancel()
        async with self._lock:
            await self._close_conn()

    # ------------------------------------------------------------------- rpc
    async def rpc(self, method: str, params: Any = None, timeout: float = 10.0) -> Any:
        await self.ensure_connected()
        assert self._conn is not None
        mid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": mid}
        if params is not None:
            msg["params"] = params
        self._conn.dc.send(json.dumps(msg))
        try:
            resp = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            raise ArkKVMError(f"RPC {method} timed out") from None
        if "error" in resp and resp["error"]:
            err = resp["error"]
            if isinstance(err, dict):
                detail = err.get("message", "")
                if err.get("data"):
                    detail = f"{detail}: {err['data']}" if detail else str(err["data"])
            else:
                detail = str(err)
            raise ArkKVMError(f"RPC {method} failed: {detail}")
        return resp.get("result")

    async def notify(self, method: str, params: Any) -> None:
        """Fire-and-forget RPC (used for HID reports to keep input snappy)."""
        await self.ensure_connected()
        assert self._conn is not None
        self._conn.dc.send(json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": next(self._ids)}))

    # ------------------------------------------------------------------ http
    def _http(self, timeout: float = 60.0) -> httpx.AsyncClient:
        headers = {"X-CSRF-Token": self._csrf} if self._csrf else {}
        return httpx.AsyncClient(
            base_url=self.http_base, verify=self.verify_tls, timeout=timeout,
            cookies=self._cookies, headers=headers,
        )

    # ------------------------------------------------- virtual USB drive (FT)
    async def ft_state(self) -> str:
        return (await self.rpc("getFileTransferState") or {}).get("target", "None")

    async def ft_attach(self, target: str) -> None:
        """target: 'Kvm' (files accessible via API), 'RemoteUsb' (USB stick on the host), 'None'."""
        if target == "None":
            await self.rpc("unmountWithFileImg", timeout=30)
        else:
            await self.rpc("mountWithFileImg", {"target": target}, timeout=30)

    async def ft_upload(self, path: str, name: str, data: bytes) -> None:
        await self.ensure_connected()
        up = await self.rpc("startFtFsUpload", {"path": path, "name": name, "size": len(data)}, timeout=30)
        upload_id = up.get("dataChannel")
        offset = int(up.get("alreadyUploadedBytes") or 0)
        async with self._http(timeout=600) as c:
            r = await c.post("/ft/upload", params={"uploadId": upload_id}, content=data[offset:])
        if r.status_code not in (200, 202):
            raise ArkKVMError(f"Upload failed: HTTP {r.status_code} {r.text[:200]}")
        if r.status_code == 202:
            raise ArkKVMError(f"Upload incomplete: {r.text[:200]}")

    async def ft_download(self, path: str, name: str) -> bytes:
        await self.ensure_connected()
        async with self._http(timeout=600) as c:
            r = await c.get("/ft/file", params={"path": path, "name": name})
        if r.status_code != 200:
            raise ArkKVMError(f"Download failed: HTTP {r.status_code} {r.text[:200]}")
        return r.content

    # ----------------------------------------------------------------- video
    async def video_state(self) -> dict:
        return await self.rpc("getVideoState")

    async def grab_frame(self, fresh: bool = True, timeout: float = 5.0) -> Frame:
        await self.ensure_connected()
        state = await self.video_state()
        if not state.get("ready"):
            raise ArkKVMError(f"No video signal: {state.get('error') or 'not ready'} ({state})")
        start_seq = self._frame_seq if (fresh and self._frame) else -1
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._frame and self._frame.seq > start_seq:
                return self._frame
            self._frame_event.clear()
            try:
                await asyncio.wait_for(self._frame_event.wait(), max(0.05, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
        if self._frame:  # static screen: encoder may not emit new frames, use the latest
            return self._frame
        raise ArkKVMError("Video signal present but no frame decoded yet; try again in a moment.")

    async def grab_stable(
        self, max_wait: float = 2.0, interval: float = 0.12, threshold: float = 0.6, needed: int = 2
    ) -> tuple[Frame, bool]:
        """Wait until the picture stops changing (animations finished, encoder refined).

        Returns (frame, stable). Gives up after max_wait and returns the latest frame.
        """
        frame = await self.grab_frame()
        prev = _thumb(frame.image)
        calm = 0
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            cur_frame = self._frame or frame
            cur = _thumb(cur_frame.image)
            diff = _mean_diff(prev, cur)
            prev, frame = cur, cur_frame
            calm = calm + 1 if diff < threshold else 0
            if calm >= needed:
                return frame, True
        return frame, False

    @staticmethod
    def encode_jpeg(image, max_width: int | None = None, quality: int = 75) -> tuple[bytes, tuple[int, int]]:  # noqa: ANN001
        img = image
        if max_width and img.width > max_width:
            h = round(img.height * max_width / img.width)
            img = img.resize((max_width, h))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=quality)
        return buf.getvalue(), img.size

    # ----------------------------------------------------------------- input
    # HID reports are awaited one by one: the firmware handles every message on the "rpc"
    # channel in its own task, so fire-and-forget reports can overtake each other.
    async def keyboard_report(self, modifier: int, keys: list[int]) -> None:
        await self.rpc("keyboardReport", {"modifier": modifier, "keys": keys[:6]}, timeout=5)

    async def abs_mouse(self, x: int, y: int, buttons: int = 0) -> None:
        x = max(0, min(ABS_MAX, int(x)))
        y = max(0, min(ABS_MAX, int(y)))
        await self.rpc("absMouseReport", {"x": x, "y": y, "buttons": buttons}, timeout=5)

    async def wheel(self, dy: int, dx: int = 0) -> None:
        await self.rpc("wheelReport", {"wheelY": max(-127, min(127, dy)), "wheelX": max(-127, min(127, dx))}, timeout=5)
