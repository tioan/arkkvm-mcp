# arkkvm-mcp

MCP server for [ArkKVM](https://www.arkkvm.com) IP-KVM devices. It lets an AI agent see and
operate a physical computer below the OS level (BIOS/UEFI, boot menus, installers, login
screens): HDMI screenshots, USB keyboard and mouse, ATX power buttons, Wake-on-LAN and
virtual media.

It talks to the device directly the same way the web UI does: WebRTC with signaling over
`/webrtc/signaling/client`, the H.264 video track decoded locally with PyAV, and JSON-RPC
on the `rpc` data channel.

## Tools

| Tool | Purpose |
| --- | --- |
| `screenshot(max_width=1280, wait_ms=0)` | JPEG of the HDMI input; mouse tools use this image's pixel coordinates |
| `click(x, y, button, count)` / `move_mouse` / `drag` / `scroll` | Absolute USB mouse |
| `type_text(text, layout)` | Types text as keystrokes (`us` or `de` layout of the **target** OS) |
| `press_keys(keys, repeat, hold_ms)` | Combos such as `ctrl+alt+delete`, `f2`, `super+r`, `down` |
| `power(action)` | ATX `power-short`, `power-long`, `reset` (needs the ATX board; untested) |
| `wake_on_lan(mac_address)` | Sends a WOL packet from the KVM (untested) |
| `mount_media_url(url, mode, detach_usb_drive)` | Attach an HTTP(S) ISO/IMG as CD-ROM or disk (streamed, needs a server with range requests) |
| `list_media` / `mount_media_storage(filename, mode, detach_usb_drive)` | Images stored on the KVM itself |
| `unmount_media` | Detach the current virtual media |
| `usb_drive_status` / `usb_drive_attach(target)` | Virtual USB Drive (file transfer): attach to `host`, `kvm` or `none` |
| `usb_drive_list` / `usb_drive_upload` / `usb_drive_download` / `usb_drive_delete` | Manage files on the Virtual USB Drive (needs it attached to the KVM) |
| `status` | Firmware, video signal, USB, virtual media, LEDs |
| `rpc_call(method, params)` | Any device JSON-RPC method (destructive ones blocked by default) |
| `release_session` | Disconnects so the web UI can be used |

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `ARKKVM_HOST` | – (required) | `192.168.1.50` or `https://arkkvm.example` |
| `ARKKVM_PASSWORD` | – | Required when the device has a local password (auth mode is detected automatically) |
| `ARKKVM_KEYBOARD_LAYOUT` | `us` | Default layout for `type_text` (`us`, `de`) |
| `ARKKVM_IDLE_TIMEOUT` | `120` | Seconds without tool calls before the session is released (0 = never) |
| `ARKKVM_KEY_DELAY_MS` | `15` | Delay between HID reports |
| `ARKKVM_SCREENSHOT_WIDTH` | `1280` | Default screenshot width and coordinate space before the first screenshot |
| `ARKKVM_SETTLE_MAX_MS` | `2000` | Max time `screenshot` waits for the picture to stop changing (animations, H.264 sharpening) |
| `ARKKVM_ALLOW_DANGEROUS_RPC` | – | `1` allows factory reset, network changes, … through `rpc_call` |
| `ARKKVM_VERIFY_TLS` | – | `1` to verify the device certificate when using `https://` |

## Usage

Requires [uv](https://docs.astral.sh/uv/) (Python 3.12+ is fetched automatically).

```sh
git clone https://github.com/tioan/arkkvm-mcp ~/Projects/arkkvm-mcp
```
 The machine running the server needs UDP access
to the KVM (WebRTC), so run it on the same LAN or over a VPN.

Claude Code:

```sh
claude mcp add arkkvm -e ARKKVM_HOST=192.168.1.50 -e ARKKVM_PASSWORD='<password>' -e ARKKVM_KEYBOARD_LAYOUT=de -- uv run --directory ~/Projects/arkkvm-mcp arkkvm-mcp
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "arkkvm": {
      "command": "uv",
      "args": ["run", "--directory", "/Users/<you>/Projects/arkkvm-mcp", "arkkvm-mcp"],
      "env": { "ARKKVM_HOST": "192.168.1.50", "ARKKVM_PASSWORD": "<password>", "ARKKVM_KEYBOARD_LAYOUT": "de" }
    }
  }
}
```

## Notes

* **Session takeover:** the device allows a single active session. A tool call takes the
  session over from an open browser tab (and vice versa); the server reconnects
  automatically on the next call and releases the session after the idle timeout.
* `type_text` sends key positions, so `layout` must match the keyboard layout configured on
  the target OS. BIOS/UEFI setup screens normally use `us`.
* `screenshot` fails with `hdmi_disconnected` when no video source is attached.
* Virtual media is read-only in both modes. `cdrom` shows an optical drive (best for BIOS/UEFI
  and Windows installers); `disk` shows a USB stick (faster; a hybrid ISO then appears as a
  disk without drive letter in Windows). Switching mode re-enumerates all KVM USB devices for
  about a second and is refused while the KVM's file-transfer "Virtual USB Drive" is attached
  to the host; pass `detach_usb_drive=true` to detach it first. Windows may reassign drive
  letters afterwards.
* Firmware quirk: after a refused mode switch `getMassStorageMode` already reports the new
  mode while the host still sees the old one. Recover by switching to the other mode and back.
* Tested against ArkKVM firmware 0.1.26050820 and 0.5.260914221 (system 0.5.26091413) with a Windows 11 target (1920x1080@60, German layout).

## Test status

Tested against a real ArkKVM (firmware 0.5.260914221 / system 0.5.26091413) with a Lenovo
ThinkCentre M720q running Windows 11 (1920x1080@60, German layout):

| Feature | Status |
| --- | --- |
| Connection, login (no password and password mode), session takeover/reconnect | tested |
| Screenshots (Windows desktop, Lenovo POST screen, BIOS setup) | tested |
| Keyboard: text (DE/US layouts), key combos, BIOS navigation | tested |
| Mouse: move, left/right/double click, drag, scroll (OS and BIOS) | tested |
| Reboot into BIOS setup via POST key (F1 on Lenovo) | tested (manual timing, no dedicated tool yet) |
| Virtual media: CD-ROM and disk mode, from URL and from KVM storage | tested |
| Virtual USB Drive: upload, download, list, delete, attach host/KVM | tested |
| Wake-on-LAN (`wake_on_lan`) | not tested |
| ATX power control (`power`) | **not tested**: no ATX extension board available |
| Audio (HDMI audio to KVM, virtual microphone to target) | **not tested**, and not exposed as tools yet |

## Virtual USB Drive (file transfer)

The KVM has a FAT-formatted drive (~16 GB) for moving files to and from the target. It is
attached to exactly one side at a time:

* `host`: the target sees a normal USB stick (BIOS/UEFI and OS, no drivers needed).
* `kvm`: the drive is detached from the target and the `usb_drive_*` file tools work.

Typical flow to hand a file to the target: `usb_drive_upload(..., switch_to_kvm=true)` and then
`usb_drive_attach("host")`. Reverse it to collect logs from the target. The file tools refuse
to detach the drive from the host unless `switch_to_kvm=true` is passed, so a running copy on
the target is not cut off by accident. Folders are deleted only with `recursive=true` (the
firmware always deletes recursively). Tested: Windows-written file read on the KVM, 5 MiB
random file round trip with matching SHA-256 on both sides, ~8 MiB/s upload over LAN.

Notes:
* Wait a few seconds after `usb_drive_attach("host")` before the target OS shows the drive.
  Drive letters can change when the KVM re-enumerates its USB devices.
* While this drive is attached to the host, the KVM refuses to switch virtual media between
  CD-ROM and disk mode (see `detach_usb_drive`).
* Formatting or repairing the drive is not exposed; reformatting it on the target may break it
  for the KVM (vendor docs).

## Input reliability

The firmware handles every message on the `rpc` data channel in its own task, so HID reports
sent without waiting can overtake each other (observed: `arkkvm` typed as `rakkvm`). This server
therefore waits for the device's acknowledgement of each keyboard/mouse report. Typing runs at
roughly 15 characters per second.

## Credits

This is an independent project, not affiliated with or endorsed by Ark Intelligent.

- **[ArkKVM](https://www.arkkvm.com)** by Ark Intelligent – the hardware and firmware this
  server talks to. The protocol (WebRTC signaling, JSON-RPC methods and parameters, auth/CSRF,
  virtual media and file transfer endpoints) was derived by reading the open-source
  [arkkvm-app](https://github.com/arkkvm/arkkvm-app) and
  [arkkvm-app-frontend](https://github.com/arkkvm/arkkvm-app-frontend) (GPL-2.0-or-later).
  No code from these repositories is included here.
- **[JetKVM](https://github.com/jetkvm/kvm)** – the ArkKVM firmware follows JetKVM's API
  design, which made the device easy to approach.
- **[jetkvm-mcp](https://github.com/shvartzj1/jetkvm-mcp)** by shvartzj1 – inspired the
  approach of driving a KVM directly over WebRTC with aiortc and decoding screenshots locally.
- Built on [aiortc](https://github.com/aiortc/aiortc), [PyAV](https://github.com/PyAV-Org/PyAV),
  [Pillow](https://python-pillow.org) and the
  [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk).

## License

MIT
