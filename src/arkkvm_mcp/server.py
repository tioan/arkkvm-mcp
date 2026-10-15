"""MCP server exposing an ArkKVM device: screen, keyboard, mouse, power, virtual media."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, Literal

try:  # mcp >= 2.0
    from mcp.server.mcpserver import Image
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP, Image

from .client import ABS_MAX, ArkKVM, ArkKVMError
from .keymap import parse_combo, text_to_strokes

logging.basicConfig(
    stream=sys.stderr,
    level=os.environ.get("ARKKVM_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
for noisy in ("aioice", "aiortc", "websockets", "httpx", "aiortc.codecs.h264"):
    logging.getLogger(noisy).setLevel(logging.ERROR if noisy.startswith("aiortc") else logging.WARNING)

HOST = os.environ.get("ARKKVM_HOST", "")
LAYOUT = os.environ.get("ARKKVM_KEYBOARD_LAYOUT", "us").lower()
KEY_DELAY = float(os.environ.get("ARKKVM_KEY_DELAY_MS", "15")) / 1000
DEFAULT_MAX_WIDTH = int(os.environ.get("ARKKVM_SCREENSHOT_WIDTH", "1280"))
SETTLE_MAX_MS = int(os.environ.get("ARKKVM_SETTLE_MAX_MS", "2000"))
ALLOW_DANGEROUS = os.environ.get("ARKKVM_ALLOW_DANGEROUS_RPC", "") in ("1", "true", "yes")

# RPC methods blocked in rpc_call unless ARKKVM_ALLOW_DANGEROUS_RPC=1
DANGEROUS_RPC = {
    "facrotyReset", "factoryReset", "resetConfig", "resetNetworkFactory", "deregisterDevice",
    "tryUpdate", "formatFileTransfer", "setNetworkSettings", "setNetworkIpAddress",
    "setVlanSettings", "setTLSState", "setSSHKeyState", "setCloudUrl", "setCloudState",
    "setDevModeState", "setDevChannelState", "setUsbConfig", "setEDID", "deleteStorageFile",
    "ftDelDir", "ftDelFile", "repairFileTransfer", "setFileTransfer", "registerTailscale", "registerTailscaleForce", "switchTailscale",
}

kvm: ArkKVM | None = None
_last_shot: tuple[int, int] | None = None  # size of the last returned screenshot (coordinate space)


@asynccontextmanager
async def lifespan(_server: FastMCP):
    global kvm
    if not HOST:
        raise SystemExit("ARKKVM_HOST is not set (e.g. ARKKVM_HOST=192.168.1.50)")
    kvm = ArkKVM(
        HOST,
        password=os.environ.get("ARKKVM_PASSWORD"),
        idle_timeout=float(os.environ.get("ARKKVM_IDLE_TIMEOUT", "120")),
        verify_tls=os.environ.get("ARKKVM_VERIFY_TLS", "") in ("1", "true", "yes"),
    )
    try:
        yield
    finally:
        await kvm.close()


mcp = FastMCP(
    "arkkvm",
    instructions=(
        "Controls a physical computer through an ArkKVM IP-KVM (HDMI capture + USB keyboard/mouse "
        "+ ATX power). Works below the OS: BIOS/UEFI, boot menus, installers, login screens. "
        "Take a screenshot before acting and after each action to verify the result. Mouse "
        "coordinates are pixels in the most recent screenshot. Connecting takes over the KVM "
        "session from any open browser tab; the session is released after an idle timeout."
    ),
    lifespan=lifespan,
)


def _k() -> ArkKVM:
    assert kvm is not None
    return kvm


async def _to_abs(x: float, y: float) -> tuple[int, int]:
    if _last_shot:
        w, h = _last_shot
    else:  # no screenshot yet: same space a default screenshot would have
        st = await _k().video_state()
        w, h = st.get("width") or 1920, st.get("height") or 1080
        if DEFAULT_MAX_WIDTH and w > DEFAULT_MAX_WIDTH:
            w, h = DEFAULT_MAX_WIDTH, round(h * DEFAULT_MAX_WIDTH / w)
    if not (0 <= x < w and 0 <= y < h):
        raise ArkKVMError(f"Point ({x}, {y}) is outside the screen ({w}x{h})")
    return round(x * ABS_MAX / max(1, w - 1)), round(y * ABS_MAX / max(1, h - 1))


BUTTONS = {"left": 1, "right": 2, "middle": 4}


# --------------------------------------------------------------------- screen
@mcp.tool()
async def screenshot(max_width: int = DEFAULT_MAX_WIDTH, wait_ms: int = 0):
    """Capture the target computer's screen (HDMI input) as JPEG.

    max_width: downscale to this width (0 = native resolution). Mouse tools use the
    coordinate space of the most recent screenshot.
    wait_ms: extra wait before capturing. The tool already waits (up to ARKKVM_SETTLE_MAX_MS)
    until the picture stops changing, so this is rarely needed.
    """
    global _last_shot
    if wait_ms > 0:
        await asyncio.sleep(wait_ms / 1000)
    k = _k()
    # wait for animations to finish and for the H.264 encoder to sharpen the picture
    frame, stable = await k.grab_stable(max_wait=SETTLE_MAX_MS / 1000)
    data, size = ArkKVM.encode_jpeg(frame.image, max_width or None)
    _last_shot = size
    native = frame.image.size
    note = f"Screen {native[0]}x{native[1]}; image {size[0]}x{size[1]} (use image pixel coordinates for mouse tools)."
    if not stable:
        note += " Screen was still changing (video/animation); content may be mid-transition."
    return [Image(data=data, format="jpeg"), note]


# ---------------------------------------------------------------------- mouse
@mcp.tool()
async def move_mouse(x: float, y: float) -> str:
    """Move the mouse pointer to (x, y) in screenshot pixel coordinates."""
    ax, ay = await _to_abs(x, y)
    await _k().abs_mouse(ax, ay, 0)
    return f"moved to ({x}, {y})"


@mcp.tool()
async def click(
    x: float, y: float, button: Literal["left", "right", "middle"] = "left", count: int = 1
) -> str:
    """Click at (x, y) in screenshot pixel coordinates. count=2 for a double click."""
    k = _k()
    ax, ay = await _to_abs(x, y)
    b = BUTTONS[button]
    await k.abs_mouse(ax, ay, 0)
    await asyncio.sleep(0.03)
    for _ in range(max(1, min(count, 3))):
        await k.abs_mouse(ax, ay, b)
        await asyncio.sleep(0.05)
        await k.abs_mouse(ax, ay, 0)
        await asyncio.sleep(0.08)
    return f"{button} click x{count} at ({x}, {y})"


@mcp.tool()
async def drag(
    x1: float, y1: float, x2: float, y2: float, button: Literal["left", "right", "middle"] = "left"
) -> str:
    """Press at (x1, y1), move to (x2, y2) and release."""
    k = _k()
    b = BUTTONS[button]
    a1, a2 = await _to_abs(x1, y1), await _to_abs(x2, y2)
    await k.abs_mouse(*a1, 0)
    await asyncio.sleep(0.03)
    await k.abs_mouse(*a1, b)
    steps = 15
    for i in range(1, steps + 1):
        await asyncio.sleep(0.02)
        await k.abs_mouse(a1[0] + (a2[0] - a1[0]) * i // steps, a1[1] + (a2[1] - a1[1]) * i // steps, b)
    await asyncio.sleep(0.05)
    await k.abs_mouse(*a2, 0)
    return f"dragged ({x1}, {y1}) -> ({x2}, {y2})"


@mcp.tool()
async def scroll(x: float, y: float, amount: int = -3, horizontal: bool = False) -> str:
    """Scroll at (x, y). Negative amount scrolls down (or left), positive up (or right)."""
    k = _k()
    ax, ay = await _to_abs(x, y)
    await k.abs_mouse(ax, ay, 0)
    step = 1 if amount > 0 else -1
    for _ in range(abs(amount)):
        await (k.wheel(0, step) if horizontal else k.wheel(step))
        await asyncio.sleep(0.03)
    return f"scrolled {amount} at ({x}, {y})"


# ------------------------------------------------------------------- keyboard
async def _stroke(mod: int, keys: list[int], hold: float = 0.0) -> None:
    k = _k()
    if mod and keys:  # press modifiers first, some BIOSes are picky
        await k.keyboard_report(mod, [])
        await asyncio.sleep(KEY_DELAY)
    await k.keyboard_report(mod, keys)
    await asyncio.sleep(max(KEY_DELAY, hold))
    await k.keyboard_report(0, [])
    await asyncio.sleep(KEY_DELAY)


@mcp.tool()
async def type_text(text: str, layout: str | None = None) -> str:
    """Type text on the target as keystrokes. '\\n' presses Enter, '\\t' Tab.

    layout: keyboard layout configured on the TARGET OS ('us' or 'de'); defaults to
    ARKKVM_KEYBOARD_LAYOUT. In BIOS/UEFI the layout is usually 'us'.
    """
    lay = (layout or LAYOUT).lower()
    try:
        strokes = text_to_strokes(text, lay)
    except ValueError as e:
        raise ArkKVMError(str(e)) from None
    for mod, code in strokes:
        await _stroke(mod, [code])
    return f"typed {len(text)} chars ({lay})"


@mcp.tool()
async def press_keys(keys: str, repeat: int = 1, hold_ms: int = 0) -> str:
    """Press a key or key combination, e.g. 'enter', 'f2', 'esc', 'ctrl+alt+delete',
    'super+r', 'alt+f4', 'shift+tab', 'down'. Keys use US physical positions.

    repeat: press the combination this many times (e.g. 'down' x5 in a boot menu).
    hold_ms: keep the keys pressed this long (e.g. to hold F12 during POST).
    """
    try:
        mod, codes = parse_combo(keys)
    except ValueError as e:
        raise ArkKVMError(str(e)) from None
    for _ in range(max(1, min(repeat, 100))):
        await _stroke(mod, codes, hold_ms / 1000)
    return f"pressed {keys} x{repeat}"


# ------------------------------------------------------------- power / media
@mcp.tool()
async def power(action: Literal["power-short", "power-long", "reset"]) -> str:
    """Press the target's ATX buttons (requires the ArkKVM ATX board wired to the mainboard).

    power-short: short press of the power button (power on / ACPI shutdown).
    power-long: hold power (~5 s, hard power off). reset: press the reset button.
    """
    await _k().rpc("setATXPowerAction", {"action": action})
    return f"ATX {action} sent"


@mcp.tool()
async def wake_on_lan(mac_address: str) -> str:
    """Send a Wake-on-LAN magic packet from the KVM to the given MAC address."""
    await _k().rpc("sendWOLMagicPacket", {"macAddress": mac_address})
    return f"WOL packet sent to {mac_address}"


async def _prepare_mode(k: ArkKVM, mode: str, detach_usb_drive: bool) -> None:
    """Switching CD-ROM <-> disk rebuilds the KVM's USB gadget (keyboard/mouse re-enumerate
    for ~1 s). The firmware rejects that while its "Virtual USB Drive" (file transfer) is
    attached to the host."""
    want = "cdrom" if mode == "cdrom" else "file"
    if await k.rpc("getMassStorageMode") == want:
        return
    ft = (await k.rpc("getFileTransferState") or {}).get("target")
    if ft == "RemoteUsb":
        if not detach_usb_drive:
            raise ArkKVMError(
                f"Switching to {mode} mode needs a USB re-enumeration, which the KVM refuses while its "
                "'Virtual USB Drive' (file transfer) is attached to the host. Retry with "
                "detach_usb_drive=true (re-attach later with rpc_call mountWithFileImg "
                "{'target': 'RemoteUsb'}) or mount in the current mode."
            )
        await k.rpc("unmountWithFileImg", timeout=20)
    await k.rpc("unmountImage", timeout=20)


@mcp.tool()
async def mount_media_url(
    url: str, mode: Literal["cdrom", "disk"] = "cdrom", detach_usb_drive: bool = False
) -> str:
    """Attach an ISO/IMG reachable over HTTP(S) as a read-only virtual USB CD-ROM or disk.

    cdrom: optical drive, most compatible in BIOS/UEFI and for Windows installers.
    disk: USB stick, faster, suits Linux/modern installers. Changing the mode re-enumerates
    the KVM's USB devices; detach_usb_drive=true allows detaching the KVM's file-transfer
    'Virtual USB Drive' from the host when that blocks the switch.
    """
    k = _k()
    # Probe first (from the KVM's point of view), before touching the current media or the
    # Virtual USB Drive: the KVM streams the image with HTTP range requests.
    try:
        check = await k.rpc("checkMountUrl", {"url": url}, timeout=30) or {}
    except ArkKVMError as e:
        check = {"Usable": False, "Reason": str(e)}
    if not check.get("Usable"):
        raise ArkKVMError(
            f"The KVM cannot use {url}: {check.get('Reason') or 'unknown reason'}. "
            "The KVM itself must reach the URL, the file must exist and the server must support "
            "HTTP range requests."
        )
    await _prepare_mode(k, mode, detach_usb_drive)
    await k.rpc("mountWithHTTP", {"url": url, "mode": mode.upper()}, timeout=30)
    return json.dumps({"mounted": await k.rpc("getVirtualMediaState"), "size": check.get("Size")})


@mcp.tool()
async def list_media() -> str:
    """List images stored on the KVM's internal storage, free space and what is mounted now."""
    k = _k()
    out = {
        "mounted": await k.rpc("getVirtualMediaState"),
        "storage": await k.rpc("getStorageSpace"),
        "files": (await k.rpc("listStorageFiles") or {}).get("files", []),
    }
    return json.dumps(out, indent=2)


@mcp.tool()
async def mount_media_storage(
    filename: str, mode: Literal["cdrom", "disk"] = "cdrom", detach_usb_drive: bool = False
) -> str:
    """Attach an image stored on the KVM (see list_media) as a read-only virtual USB CD-ROM
    or disk. Mode semantics and detach_usb_drive as in mount_media_url."""
    k = _k()
    files = [f.get("filename") for f in (await k.rpc("listStorageFiles") or {}).get("files", [])]
    if filename not in files:
        raise ArkKVMError(f"'{filename}' is not on the KVM storage. Available: {', '.join(files) or 'none'}")
    await _prepare_mode(k, mode, detach_usb_drive)
    await k.rpc("mountWithStorage", {"filename": filename, "mode": mode.upper()}, timeout=30)
    return json.dumps({"mounted": await k.rpc("getVirtualMediaState")})


@mcp.tool()
async def unmount_media() -> str:
    """Detach the currently mounted virtual media."""
    await _k().rpc("unmountImage", timeout=20)
    return "virtual media unmounted"


# ------------------------------------------------- virtual USB drive (file transfer)
_FT_TARGETS = {"host": "RemoteUsb", "kvm": "Kvm", "none": "None"}
_FT_NAMES = {v: k for k, v in _FT_TARGETS.items()}


def _split(remote_path: str) -> tuple[str, str]:
    p = "/" + remote_path.strip().lstrip("/")
    d, _, n = p.rpartition("/")
    if not n:
        raise ArkKVMError(f"'{remote_path}' is not a file path")
    return d or "/", n


async def _require_kvm(k: ArkKVM, switch: bool) -> None:
    target = await k.ft_state()
    if target == "Kvm":
        return
    if target == "RemoteUsb" and not switch:
        raise ArkKVMError(
            "The Virtual USB Drive is attached to the host, so the KVM cannot access its files. "
            "Retry with switch_to_kvm=true (detaches it from the host; make sure the host is not "
            "writing to it) or call usb_drive_attach('kvm')."
        )
    await k.ft_attach("Kvm")


@mcp.tool()
async def usb_drive_status() -> str:
    """Status of the KVM's Virtual USB Drive (FAT file-transfer drive): enabled, where it is
    attached ('host' = USB stick on the target, 'kvm' = files accessible via these tools, 'none')
    and, when attached to the KVM, its capacity."""
    k = _k()
    target = await k.ft_state()
    out: dict[str, Any] = {"enabled": await k.rpc("getFileTransfer"), "attached_to": _FT_NAMES.get(target, target)}
    if target == "Kvm":
        out["filesystem"] = await k.rpc("ftFileSystemInfo")
    return json.dumps(out, indent=2)


@mcp.tool()
async def usb_drive_attach(target: Literal["host", "kvm", "none"]) -> str:
    """Attach the Virtual USB Drive to the target computer ('host', appears as a USB stick), to the
    KVM ('kvm', needed for usb_drive_list/upload/download/delete) or detach it ('none').
    The drive is only ever attached to one side. After attaching to the host, wait a few
    seconds before the OS shows it; drive letters can change."""
    k = _k()
    await k.ft_attach(_FT_TARGETS[target])
    return json.dumps({"attached_to": _FT_NAMES.get(await k.ft_state())})


@mcp.tool()
async def usb_drive_list(path: str = "/", switch_to_kvm: bool = False) -> str:
    """List a directory on the Virtual USB Drive. Requires the drive attached to the KVM."""
    k = _k()
    await _require_kvm(k, switch_to_kvm)
    return json.dumps(await k.rpc("ftListFile", {"path": "/" + path.strip().lstrip("/")}), indent=2)


@mcp.tool()
async def usb_drive_upload(
    remote_path: str, local_path: str | None = None, content: str | None = None, switch_to_kvm: bool = False
) -> str:
    """Copy a file onto the Virtual USB Drive, e.g. remote_path='/drivers/setup.exe'.
    Source is either local_path (a file on the machine running this MCP server) or content
    (text, written as UTF-8). Missing parent folders are created."""
    if (local_path is None) == (content is None):
        raise ArkKVMError("Pass exactly one of local_path or content")
    data = Path(local_path).expanduser().read_bytes() if local_path else content.encode("utf-8")
    k = _k()
    await _require_kvm(k, switch_to_kvm)
    d, n = _split(remote_path)
    cur = ""
    for part in [x for x in d.split("/") if x]:
        cur += "/" + part
        try:
            await k.rpc("ftCreateDir", {"path": cur})
        except ArkKVMError:
            pass  # already exists
    await k.ft_upload(d, n, data)
    return json.dumps({"uploaded": f"{d.rstrip('/')}/{n}", "bytes": len(data)})


@mcp.tool()
async def usb_drive_download(remote_path: str, local_path: str | None = None, switch_to_kvm: bool = False) -> str:
    """Read a file from the Virtual USB Drive. With local_path it is saved there (on the machine
    running this MCP server); without, text files up to 100 KB are returned inline."""
    k = _k()
    await _require_kvm(k, switch_to_kvm)
    d, n = _split(remote_path)
    data = await k.ft_download(d, n)
    if local_path:
        dest = Path(local_path).expanduser()
        if dest.is_dir():
            dest = dest / n
        dest.write_bytes(data)
        return json.dumps({"saved": str(dest), "bytes": len(data)})
    if len(data) > 100_000:
        raise ArkKVMError(f"{len(data)} bytes: too large to return inline, pass local_path")
    if b"\x00" in data:
        raise ArkKVMError("Binary file: pass local_path to save it")
    for enc in ("utf-8", "cp1252", "cp850"):  # cp850: Windows console (cmd echo > file)
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ArkKVMError("Binary file: pass local_path to save it")


@mcp.tool()
async def usb_drive_delete(remote_path: str, recursive: bool = False, switch_to_kvm: bool = False) -> str:
    """Delete a file or folder on the Virtual USB Drive. Folders that are not empty are only
    deleted with recursive=true (the firmware deletes folders including their contents)."""
    k = _k()
    await _require_kvm(k, switch_to_kvm)
    p = "/" + remote_path.strip().lstrip("/")
    d, n = _split(p)
    entries = await k.rpc("ftListFile", {"path": d}) or []
    entry = next((e for e in entries if e.get("name") == n), None)
    if entry is None:
        raise ArkKVMError(f"'{p}' not found")
    if entry.get("is_dir"):
        children = await k.rpc("ftListFile", {"path": p}) or []
        children = [c for c in children if c.get("name") not in (".", "..")]
        if children and not recursive:
            raise ArkKVMError(f"'{p}' contains {len(children)} entries; pass recursive=true to delete it with its contents")
        await k.rpc("ftDelDir", {"path": p})
    else:
        await k.rpc("ftDelFile", {"path": p})
    return json.dumps({"deleted": p})


@mcp.tool()
async def status() -> str:
    """Report KVM status: firmware, HDMI/video signal, USB attachment, virtual media,
    keyboard LEDs and last known ATX LED state."""
    k = _k()
    out: dict[str, Any] = {"host": k.http_base}
    for key, method in [
        ("version", "getCurrentVersion"), ("video", "getVideoState"), ("usb", "getUSBState"),
        ("virtualMedia", "getVirtualMediaState"), ("keyboardLeds", "getKeyboardLedState"),
        ("jiggler", "getJigglerState"),
    ]:
        try:
            out[key] = await k.rpc(method)
        except ArkKVMError as e:
            out[key] = f"error: {e}"
    out["atx"] = k.events.get("atxState", "unknown (no ATX event received)")
    return json.dumps(out, indent=2)


@mcp.tool()
async def rpc_call(method: str, params: dict | None = None) -> str:
    """Advanced: call any ArkKVM JSON-RPC method (e.g. getNetworkState, getEDID,
    getStorageSpace, listStorageFiles, setJigglerState). Destructive/configuration
    methods are blocked unless ARKKVM_ALLOW_DANGEROUS_RPC=1."""
    if method in DANGEROUS_RPC and not ALLOW_DANGEROUS:
        raise ArkKVMError(f"'{method}' is blocked (set ARKKVM_ALLOW_DANGEROUS_RPC=1 to allow)")
    res = await _k().rpc(method, params, timeout=30)
    return json.dumps(res, indent=2, default=str)


@mcp.tool()
async def release_session() -> str:
    """Disconnect from the KVM now so the web UI can be used without being taken over."""
    await _k().close()
    return "session released"


def main() -> None:
    if sys.stdin.isatty():
        print(
            f"arkkvm-mcp: MCP server for {HOST or '<ARKKVM_HOST not set>'} waiting on stdio "
            "(started by an MCP client such as Claude Desktop; Ctrl+C to quit)",
            file=sys.stderr,
        )
    # anyio's stdio reader thread blocks on stdin and keeps the process alive after Ctrl+C,
    # so exit right away. The KVM drops the WebRTC session when the WebSocket closes.
    import signal

    def _quit(signum, _frame):  # noqa: ANN001
        if sys.stderr.isatty():
            sys.stderr.write("\n")  # end the "^C" line cleanly in the terminal
            sys.stderr.flush()
        os._exit(128 + signum)

    signal.signal(signal.SIGINT, _quit)
    signal.signal(signal.SIGTERM, _quit)
    mcp.run()


if __name__ == "__main__":
    main()
