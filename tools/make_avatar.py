#!/usr/bin/env python3
"""Generate Cubie's face as a stackchan-mcp `layered` avatar set.

Why this exists
---------------
The stackchan-mcp release firmware ships `avatar_images.cc` as a pure black
RGB565 placeholder -- "the firmware builds and runs, but the screen will
display nothing". Real art normally means supplying PNGs and rebuilding with
Docker + ESP-IDF. The gateway's `load_avatar_set` avoids that: it stages a raw
RGB565 payload over HTTP, the device fetches, SHA256-verifies and loads it into
PSRAM. So the face is our asset, pushed at runtime.

PSRAM is volatile, so the set does not survive a reboot -- pushing it belongs
in the bridge's connect sequence.

Fidelity
--------
The face M5's factory firmware drew is **M5Stack-Avatar** (Shinya Ishikawa,
MIT). It is a live vector renderer, and the shapes below are transcribed from
its source rather than eyeballed:

  Eye::draw   open   -> fillCircle(x, y, r)
              closed -> fillRect(x - r, y - 2, r * 2, 4)      # a flat BAR
              Happy  -> circle, minus a bg rect over the lower half,
                        minus a bg circle of r/1.5 at the centre
              Sad    -> circle, minus a bg triangle across the top,
                        apex toward the outer edge
  Mouth::draw          fillRect, w = minW + (maxW-minW)*(1-open)
                                 h = minH + (maxH-minH)*open   # a RECTANGLE

Default layout, in the library's own 320x240 screen space -- note it is
deliberately asymmetric, and that asymmetry is part of the character:

  eyeR (x=90,  y=93)   eyeL (x=230, y=96)   mouth (x=163, y=148)
  Mouth(minW=50, maxW=90, minH=4, maxH=60)

What this CANNOT reproduce: the original is continuous -- breath, gaze
saccades, smooth blink and mouth-open interpolation. `load_avatar_set` takes
14 discrete frames (or 90 in matrix mode). We can match the *look* exactly;
matching the *motion* would mean porting the renderer into firmware.

Format (from firmware/scripts/avatar_convert/convert_avatars.py):
  160x120, little-endian RGB565, no row padding, 38,400 bytes per frame.
  `layered` = 14 frames, 537,600 bytes, in this exact order:
      idle happy thinking sad surprised embarrassed
      eyes_open eyes_half eyes_closed
      mouth_closed mouth_half mouth_open mouth_e mouth_u
  Every frame is a FULL face: the firmware header calls the eye and mouth
  entries a "full-frame swap", not an alpha overlay.

No PIL dependency. Rendered in 320x240 library space at 2x, then box-
downsampled to 160x120, which mirrors the upstream converter's own pipeline.
"""

import hashlib, struct, sys, zlib
from pathlib import Path

W, H = 160, 120                  # output frame
SCALE = 2.0                      # library space (320x240) -> output
SS = 2                           # supersamples per output pixel, per axis

# --- M5Stack-Avatar default geometry, in 320x240 library space -------------
EYE_R_X, EYE_R_Y = 90.0, 93.0    # right eye centre (screen left)
EYE_L_X, EYE_L_Y = 230.0, 96.0   # left eye centre  (screen right)
MOUTH_X, MOUTH_Y = 163.0, 148.0
MOUTH_MIN_W, MOUTH_MAX_W = 50.0, 90.0
MOUTH_MIN_H, MOUTH_MAX_H = 4.0, 60.0

# The library's own default is Eye(8), which is a very small dot on a 320px
# screen. M5's shipped StackChan face uses noticeably larger eyes, so this is
# the one number chosen by eye rather than transcribed. Change it here.
EYE_R = 26.0

ORDER = ["idle", "happy", "thinking", "sad", "surprised", "embarrassed",
         "eyes_open", "eyes_half", "eyes_closed",
         "mouth_closed", "mouth_half", "mouth_open", "mouth_e", "mouth_u"]


def in_circle(x, y, cx, cy, r):
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def in_rect(x, y, left, top, w, h):
    return left <= x <= left + w and top <= y <= top + h


def in_tri(x, y, p0, p1, p2):
    def side(a, b):
        return (b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0])
    d0, d1, d2 = side(p0, p1), side(p1, p2), side(p2, p0)
    return (d0 >= 0 and d1 >= 0 and d2 >= 0) or (d0 <= 0 and d1 <= 0 and d2 <= 0)


def eye_on(x, y, cx, cy, r, mode, is_left):
    """True where the eye paints foreground. Mirrors Eye::draw."""
    if mode == "closed":                                   # flat bar
        return in_rect(x, y, cx - r, cy - 2, r * 2, 4)
    if not in_circle(x, y, cx, cy, r):
        return False
    if mode == "happy":                                    # crescent
        if in_rect(x, y, cx - r, cy + r * 0.0, r * 2 + 4, r + 2):
            return False
        if in_circle(x, y, cx, cy, r / 1.5):
            return False
        return True
    if mode == "sleepy":                                   # lower half cut
        return not in_rect(x, y, cx - r, cy, r * 2 + 4, r + 2)
    if mode == "half":                                     # upper half cut
        return not in_rect(x, y, cx - r - 2, cy - r - 2, r * 2 + 4, r + 2)
    if mode == "sad":                                      # triangle off the top
        x0, y0 = cx - r, cy - r
        x1, y1 = x0 + r * 2, y0
        x2 = x1 if is_left else x0                         # apex toward outer edge
        return not in_tri(x, y, (x0, y0), (x1, y1), (x2, y0 + r))
    return True                                            # "open"


def mouth_on(x, y, open_ratio, w_scale=1.0):
    """True inside the mouth rect. Mirrors Mouth::draw."""
    h = MOUTH_MIN_H + (MOUTH_MAX_H - MOUTH_MIN_H) * open_ratio
    w = (MOUTH_MIN_W + (MOUTH_MAX_W - MOUTH_MIN_W) * (1 - open_ratio)) * w_scale
    return in_rect(x, y, MOUTH_X - w / 2, MOUTH_Y - h / 2, w, h)


# name -> (eye mode, mouth open ratio, eye radius x, gaze dy, mouth width x)
FRAMES = {
    "idle":         ("open",   0.00, 1.00,  0, 1.00),
    "happy":        ("happy",  0.35, 1.00,  0, 1.00),
    "thinking":     ("open",   0.10, 1.00, -6, 0.70),
    "sad":          ("sad",    0.00, 1.00,  4, 0.85),
    "surprised":    ("open",   0.80, 1.22, -4, 1.00),
    "embarrassed":  ("sleepy", 0.15, 1.00,  2, 1.00),
    "eyes_open":    ("open",   0.00, 1.00,  0, 1.00),
    "eyes_half":    ("half",   0.00, 1.00,  0, 1.00),
    "eyes_closed":  ("closed", 0.00, 1.00,  0, 1.00),
    "mouth_closed": ("open",   0.00, 1.00,  0, 1.00),
    "mouth_half":   ("open",   0.50, 1.00,  0, 1.00),
    "mouth_open":   ("open",   1.00, 1.00,  0, 1.00),
    "mouth_e":      ("open",   0.15, 1.00,  0, 1.00),
    "mouth_u":      ("open",   0.60, 1.00,  0, 0.62),
}


def render(name):
    mode, opening, r_mul, dy, w_mul = FRAMES[name]
    r = EYE_R * r_mul
    lum = bytearray(W * H)
    inv = 1.0 / (SS * SS)
    step = 1.0 / SS
    for oy in range(H):
        for ox in range(W):
            hits = 0
            for sy in range(SS):
                y = (oy + (sy + 0.5) * step) * SCALE
                for sx in range(SS):
                    x = (ox + (sx + 0.5) * step) * SCALE
                    if (eye_on(x, y - dy, EYE_R_X, EYE_R_Y, r, mode, False)
                            or eye_on(x, y - dy, EYE_L_X, EYE_L_Y, r, mode, True)
                            or mouth_on(x, y, opening, w_mul)):
                        hits += 1
            lum[oy * W + ox] = int(255 * hits * inv)
    return lum


def to_rgb565(lum):
    out = bytearray(len(lum) * 2)
    for i, v in enumerate(lum):
        p = ((v & 0xF8) << 8) | ((v & 0xFC) << 3) | (v >> 3)
        out[i * 2] = p & 0xFF
        out[i * 2 + 1] = (p >> 8) & 0xFF
    return bytes(out)


def write_png(path, w, h, rgb):
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))
    raw = b"".join(b"\x00" + rgb[y * w * 3:(y + 1) * w * 3] for y in range(h))
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def contact_sheet(frames, path):
    rows, gap, cols = [ORDER[:6], ORDER[6:9], ORDER[9:]], 3, 6
    sw, sh = cols * W + (cols + 1) * gap, len(rows) * H + (len(rows) + 1) * gap
    buf = bytearray([40]) * (sw * sh * 3)
    for r, row in enumerate(rows):
        for c, name in enumerate(row):
            lum = frames[name]
            ox, oy = gap + c * (W + gap), gap + r * (H + gap)
            for y in range(H):
                base = ((oy + y) * sw + ox) * 3
                for x in range(W):
                    v = lum[y * W + x]
                    buf[base + x * 3] = buf[base + x * 3 + 1] = buf[base + x * 3 + 2] = v
    write_png(path, sw, sh, bytes(buf))


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    frames = {n: render(n) for n in ORDER}
    payload = b"".join(to_rgb565(frames[n]) for n in ORDER)
    expected = 14 * W * H * 2
    assert len(payload) == expected, f"{len(payload)} != {expected}"
    (out / "cubie-avatar-layered.rgb565").write_bytes(payload)
    contact_sheet(frames, out / "cubie-avatar-preview.png")
    print(f"frames  : {len(ORDER)}")
    print(f"bytes   : {len(payload)} (expected {expected})")
    print(f"sha256  : {hashlib.sha256(payload).hexdigest()}")
    print(f"written : {out / 'cubie-avatar-layered.rgb565'}")
    print(f"preview : {out / 'cubie-avatar-preview.png'}")


if __name__ == "__main__":
    main()
