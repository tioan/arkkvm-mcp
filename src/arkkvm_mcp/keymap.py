"""USB HID usage codes, key-combo parsing and text-to-HID layouts (US, DE)."""

from __future__ import annotations

# Modifier bits in the HID keyboard report
MOD = {
    "ctrl": 0x01, "control": 0x01, "lctrl": 0x01,
    "shift": 0x02, "lshift": 0x02,
    "alt": 0x04, "lalt": 0x04, "option": 0x04,
    "meta": 0x08, "super": 0x08, "win": 0x08, "windows": 0x08, "cmd": 0x08, "command": 0x08, "gui": 0x08,
    "rctrl": 0x10, "rshift": 0x20,
    "ralt": 0x40, "altgr": 0x40,
    "rmeta": 0x80, "rsuper": 0x80,
}
SHIFT, ALTGR = 0x02, 0x40

KEYS: dict[str, int] = {}
for i, ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
    KEYS[ch] = 0x04 + i
for i, ch in enumerate("1234567890"):
    KEYS[ch] = 0x1E + i
for i in range(1, 13):
    KEYS[f"f{i}"] = 0x39 + i
for i in range(13, 25):
    KEYS[f"f{i}"] = 0x68 + (i - 13)
KEYS.update({
    "enter": 0x28, "return": 0x28, "esc": 0x29, "escape": 0x29, "backspace": 0x2A,
    "tab": 0x2B, "space": 0x2C, "minus": 0x2D, "equal": 0x2E, "leftbracket": 0x2F,
    "rightbracket": 0x30, "backslash": 0x31, "hash": 0x32, "semicolon": 0x33, "quote": 0x34,
    "grave": 0x35, "comma": 0x36, "period": 0x37, "slash": 0x38, "capslock": 0x39,
    "printscreen": 0x46, "prtsc": 0x46, "sysrq": 0x46, "scrolllock": 0x47, "pause": 0x48,
    "break": 0x48, "insert": 0x49, "ins": 0x49, "home": 0x4A, "pageup": 0x4B, "pgup": 0x4B,
    "delete": 0x4C, "del": 0x4C, "end": 0x4D, "pagedown": 0x4E, "pgdn": 0x4E,
    "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52, "numlock": 0x53,
    "kp_divide": 0x54, "kp_multiply": 0x55, "kp_minus": 0x56, "kp_plus": 0x57,
    "kp_enter": 0x58, "kp_0": 0x62, "kp_period": 0x63, "102nd": 0x64, "lessgreater": 0x64,
    "menu": 0x65, "application": 0x65, "contextmenu": 0x65,
})
for i in range(1, 10):
    KEYS[f"kp_{i}"] = 0x58 + i


def parse_combo(combo: str) -> tuple[int, list[int]]:
    """'ctrl+alt+delete' -> (modifier_bits, [keycodes]). Single chars use US positions."""
    mod, keys = 0, []
    parts = [p for p in combo.lower().replace(" ", "").split("+")]
    # allow a literal '+' key: "ctrl++"
    if combo.endswith("++"):
        parts = parts[:-2] + ["equal"]
    for p in parts:
        if not p:
            continue
        if p in MOD:
            mod |= MOD[p]
        elif p in KEYS:
            keys.append(KEYS[p])
        elif p.startswith("0x"):
            keys.append(int(p, 16))
        else:
            raise ValueError(f"Unknown key '{p}' in '{combo}'")
    if len(keys) > 6:
        raise ValueError("At most 6 non-modifier keys can be pressed at once")
    return mod, keys


# char -> (modifier, keycode, dead_key)
Layout = dict[str, tuple[int, int, bool]]


def _us() -> Layout:
    m: Layout = {}
    for ch in "abcdefghijklmnopqrstuvwxyz":
        m[ch] = (0, KEYS[ch], False)
        m[ch.upper()] = (SHIFT, KEYS[ch], False)
    for ch in "1234567890":
        m[ch] = (0, KEYS[ch], False)
    for ch, sh in zip("1234567890", "!@#$%^&*()"):
        m[sh] = (SHIFT, KEYS[ch], False)
    for plain, shifted, code in [
        ("-", "_", 0x2D), ("=", "+", 0x2E), ("[", "{", 0x2F), ("]", "}", 0x30),
        ("\\", "|", 0x31), (";", ":", 0x33), ("'", '"', 0x34), ("`", "~", 0x35),
        (",", "<", 0x36), (".", ">", 0x37), ("/", "?", 0x38),
    ]:
        m[plain] = (0, code, False)
        m[shifted] = (SHIFT, code, False)
    return m


def _de() -> Layout:
    m: Layout = {}
    for ch in "abcdefghijklmnopqrstuvwxyz":
        code = KEYS[{"y": "z", "z": "y"}.get(ch, ch)]
        m[ch] = (0, code, False)
        m[ch.upper()] = (SHIFT, code, False)
    for ch in "1234567890":
        m[ch] = (0, KEYS[ch], False)
    for ch, sh in zip("1234567890", '!"§$%&/()='):
        m[sh] = (SHIFT, KEYS[ch], False)
    m.update({
        "²": (ALTGR, KEYS["2"], False), "³": (ALTGR, KEYS["3"], False),
        "{": (ALTGR, KEYS["7"], False), "[": (ALTGR, KEYS["8"], False),
        "]": (ALTGR, KEYS["9"], False), "}": (ALTGR, KEYS["0"], False),
        "ß": (0, 0x2D, False), "?": (SHIFT, 0x2D, False), "\\": (ALTGR, 0x2D, False),
        "´": (0, 0x2E, True), "`": (SHIFT, 0x2E, True),
        "ü": (0, 0x2F, False), "Ü": (SHIFT, 0x2F, False),
        "+": (0, 0x30, False), "*": (SHIFT, 0x30, False), "~": (ALTGR, 0x30, False),
        "#": (0, 0x32, False), "'": (SHIFT, 0x32, False),
        "ö": (0, 0x33, False), "Ö": (SHIFT, 0x33, False),
        "ä": (0, 0x34, False), "Ä": (SHIFT, 0x34, False),
        "^": (0, 0x35, True), "°": (SHIFT, 0x35, False),
        ",": (0, 0x36, False), ";": (SHIFT, 0x36, False),
        ".": (0, 0x37, False), ":": (SHIFT, 0x37, False),
        "-": (0, 0x38, False), "_": (SHIFT, 0x38, False),
        "<": (0, 0x64, False), ">": (SHIFT, 0x64, False), "|": (ALTGR, 0x64, False),
        "@": (ALTGR, KEYS["q"], False), "€": (ALTGR, KEYS["e"], False), "µ": (ALTGR, KEYS["m"], False),
    })
    return m


_COMMON = {" ": (0, 0x2C, False), "\n": (0, 0x28, False), "\t": (0, 0x2B, False)}
LAYOUTS: dict[str, Layout] = {"us": {**_us(), **_COMMON}, "de": {**_de(), **_COMMON}}


def text_to_strokes(text: str, layout: str) -> list[tuple[int, int]]:
    """Convert text into (modifier, keycode) strokes. Dead keys get a trailing space."""
    table = LAYOUTS.get(layout.lower())
    if table is None:
        raise ValueError(f"Unknown layout '{layout}', available: {', '.join(LAYOUTS)}")
    strokes: list[tuple[int, int]] = []
    missing = sorted({c for c in text.replace("\r\n", "\n") if c not in table and c != "\r"})
    if missing:
        raise ValueError(f"Characters not typeable with layout '{layout}': {''.join(missing)!r}")
    for ch in text.replace("\r\n", "\n"):
        if ch == "\r":
            continue
        mod, code, dead = table[ch]
        strokes.append((mod, code))
        if dead:
            strokes.append((0, 0x2C))
    return strokes
