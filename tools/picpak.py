#!/usr/bin/env python3
"""PicPak BLE client — scan / probe / dispatch a picture.

Reverse-engineered from a full HCI-snoop capture; see PROTOCOL.md.

Subcommands:
  scan                       list nearby BLE devices, highlight PicPak
  probe ADDR                 connect, read device info + slot map + per-slot digests
  dispatch ADDR IMAGE [SLOT] send an image (converts to e-ink framebuffer)
  calib  ADDR IMAGE          send orientation/fit candidates to consecutive slots
  ccalib ADDR IMAGE          send a 4-color palette strip + a color-encoded image
"""
import argparse
import asyncio
import os
import sys

import numpy as np
from bleak import BleakScanner, BleakClient

OUTDIR = "captures"  # local scratch for encoded/debug output (git-ignored)


def dump(name, data):
    """Write debug/output bytes to OUTDIR, creating it on demand."""
    os.makedirs(OUTDIR, exist_ok=True)
    with open(os.path.join(OUTDIR, name), "wb") as f:
        f.write(data)

SVC = "0000ff00-0000-1000-8000-00805f9b34fb"
FF01 = "0000ff01-0000-1000-8000-00805f9b34fb"  # main cmd/data
FF02 = "0000ff02-0000-1000-8000-00805f9b34fb"  # info/config
FF03 = "0000ff03-0000-1000-8000-00805f9b34fb"
FF04 = "0000ff04-0000-1000-8000-00805f9b34fb"

START, END = 0xAA, 0xFF


def frame(cmd, body=b""):
    return bytes([START, cmd]) + bytes(body) + bytes([END])


def is_picpak(name, uuids):
    n = (name or "").lower()
    return "pic" in n or any(u.lower().startswith("0000ff00") for u in (uuids or []))


async def cmd_scan(args):
    found = {}

    def cb(d, ad):
        found[d.address] = (ad.local_name or d.name or "", ad.rssi,
                            list(ad.service_uuids or []))

    s = BleakScanner(detection_callback=cb)
    await s.start()
    await asyncio.sleep(args.timeout)
    await s.stop()
    print(f"found {len(found)} devices")
    for addr, (name, rssi, uuids) in sorted(found.items(), key=lambda x: -x[1][1]):
        mark = "  *** PICPAK" if is_picpak(name, uuids) else ""
        print(f"  {addr}  rssi={rssi:4d}  name={name!r}  {uuids}{mark}")


class PicPak:
    """Thin wrapper: send AA..FF frames, collect indications per characteristic."""

    def __init__(self, client):
        self.c = client
        self.rx = {FF01: asyncio.Queue(), FF02: asyncio.Queue(),
                   FF03: asyncio.Queue(), FF04: asyncio.Queue()}

    async def subscribe(self):
        for u in (FF01, FF02, FF03, FF04):
            def make(uu):
                def h(_, data):
                    self.rx[uu].put_nowait(bytes(data))
                return h
            try:
                await self.c.start_notify(u, make(u))
            except Exception as e:
                print(f"  (notify {u[4:8]} failed: {e})")

    async def call(self, char, cmd, body=b"", rx=None, timeout=5.0):
        """Write a frame to `char`, await one indication on `rx` (default same char)."""
        rx = rx or char
        # drain stale
        while not self.rx[rx].empty():
            self.rx[rx].get_nowait()
        await self.c.write_gatt_char(char, frame(cmd, body), response=True)
        try:
            return await asyncio.wait_for(self.rx[rx].get(), timeout)
        except asyncio.TimeoutError:
            return None


def parse_kv_frame(resp):
    if not resp or resp[0] != START or resp[-1] != END:
        return None
    return resp[1:-1]  # strip AA .. FF


async def connect(addr):
    dev = await BleakScanner.find_device_by_address(addr, timeout=15.0)
    if not dev:
        # maybe it's advertising under a name; caller can also pass a name via scan
        raise SystemExit(f"device {addr} not found advertising — press its button "
                         f"(3s) and make sure the phone's Bluetooth is OFF")
    client = BleakClient(dev)
    await client.connect()
    return client


async def cmd_probe(args):
    client = await connect(args.addr)
    try:
        print(f"connected: {client.address}")
        for s in client.services:
            if s.uuid.lower().startswith("0000ff00"):
                print(f"service {s.uuid}")
                for ch in s.characteristics:
                    print(f"   char {ch.uuid}  props={ch.properties}")
        pp = PicPak(client)
        await pp.subscribe()

        info = await pp.call(FF02, 0x08, b"\x02\x01")
        print("device info (0x08):", info.hex() if info else None)
        if info:
            body = parse_kv_frame(info)
            # ascii fields separated by NULs
            txt = bytes(b if 32 <= b < 127 else 0 for b in body)
            fields = [f.decode() for f in txt.split(b"\x00") if len(f) >= 3]
            print("   fields:", fields)

        disp = await pp.call(FF02, 0x09, b"\x02")
        print("display params (0x09):", disp.hex() if disp else None)

        slots = await pp.call(FF01, 0x34, b"\x02")
        used = 0
        if slots:
            total, used, free, bits, nxt = parse_slotmap(slots)
            print(f"slots: used={used} bitmap=0x{bits:x} next_free={nxt}  raw={slots.hex()}")

        # 0x04 = per-slot 16-byte image digest (MD5). Read the digest of each used slot.
        print("per-slot image digests (0x04):")
        for idx in range(1, (used or 0) + 1):
            r = await pp.call(FF01, 0x04, bytes([idx, 0, 0x02]))
            body = parse_kv_frame(r) if r else None
            digest = body[4:].hex() if body and len(body) >= 4 else None
            print(f"   slot {idx}: {digest}")
    finally:
        await client.disconnect()


FB_W, FB_H, FB_BPP = 400, 300, 2
FB_SIZE = FB_W * FB_H * FB_BPP // 8  # 30000
CHUNK = 236  # data bytes per frame (→245-byte ATT write), matches capture


def parse_slotmap(resp):
    """Return (used_count, free_bytes, bitmap, next_free_slot) from a 0x34 reply."""
    b = parse_kv_frame(resp)  # strips AA35 .. FF -> starts after 0x35
    # observed: <u16 total> <u16 used> <u16 free> <bitmap...>
    body = b[1:]  # drop the 0x35 status/echo byte
    total = int.from_bytes(body[0:2], "little")
    used = int.from_bytes(body[2:4], "little")
    free = int.from_bytes(body[4:6], "little")
    bitmap = body[6:]
    bits = int.from_bytes(bitmap, "little")
    nxt = next(i for i in range(1, len(bitmap) * 8) if not (bits >> (i - 1)) & 1)
    return total, used, free, bits, nxt


async def send_framebuffer(pp, fb, slot):
    import hashlib
    assert len(fb) == FB_SIZE, f"framebuffer must be {FB_SIZE} bytes, got {len(fb)}"
    n = (len(fb) + CHUNK - 1) // CHUNK
    print(f"streaming {len(fb)} bytes in {n} chunks to slot {slot}...")
    for i in range(n):
        part = fb[i * CHUNK:(i + 1) * CHUNK]
        last = (i == n - 1)
        seq = i | (0x0100 if last else 0)
        body = (slot.to_bytes(2, "little") + seq.to_bytes(2, "little")
                + len(part).to_bytes(2, "little") + part)
        # data chunks: write with response (matches capture); reply only on some
        await pp.c.write_gatt_char(FF01, frame(0x01, body), response=True)
    print("  chunks sent. writing digest + commit...")
    digest = hashlib.md5(fb).digest()
    r1 = await pp.call(FF01, 0x04, slot.to_bytes(2, "little") + b"\x00" + digest)
    print("  digest ack:", r1.hex() if r1 else "(none)")
    r2 = await pp.call(FF01, 0x36, slot.to_bytes(2, "little"))
    print("  commit ack:", r2.hex() if r2 else "(none)")
    return r2 is not None


# code order darkest->lightest. From capture: code 0 = black, code 1 = white,
# and 2/3 are the middle grays (rarely used). Default darkest->lightest = [0,2,3,1].
DEFAULT_PALETTE = (0, 2, 3, 1)


def _fit(im, mode):
    """Fit grayscale image to FB_W x FB_H. mode: cover | contain | stretch."""
    from PIL import Image, ImageOps
    if mode == "stretch":
        return im.resize((FB_W, FB_H))
    if mode == "contain":
        c = im.copy()
        c.thumbnail((FB_W, FB_H))
        bg = Image.new("L", (FB_W, FB_H), 255)  # white letterbox
        bg.paste(c, ((FB_W - c.width) // 2, (FB_H - c.height) // 2))
        return bg
    return ImageOps.fit(im, (FB_W, FB_H), method=Image.LANCZOS)  # cover


# Panel is a 400x300 BWRY 4-color e-paper (2 bits/px -> 4 colors).
# code 0 = black, 1 = white (confirmed); 2/3 = red/yellow (order set by palette test).
# RGB the quantizer maps each code to; index = code value.
PANEL_COLORS = {
    0: (0, 0, 0),        # black
    1: (255, 255, 255),  # white (renders as e-ink light grey)
    2: (235, 205, 40),   # yellow  (code order verified on-device: 0=blk 1=wht 2=ylw 3=red)
    3: (200, 30, 30),    # red
}


def _pack_codes(codes_2d):
    """codes_2d: HxW uint8 array of 2-bit codes -> packed framebuffer bytes."""
    codes = np.asarray(codes_2d, dtype=np.uint8).reshape(-1)
    out = bytearray()
    for i in range(0, len(codes), 4):
        out.append((codes[i] << 6) | (codes[i+1] << 4) | (codes[i+2] << 2) | codes[i+3])
    return bytes(out)


def make_palette_strips():
    """400x300 framebuffer: 4 vertical stripes of codes 0,1,2,3 (left->right)."""
    a = np.zeros((FB_H, FB_W), dtype=np.uint8)
    for c in range(4):
        a[:, c * 100:(c + 1) * 100] = c
    return _pack_codes(a)


def encode_color(path, rotate=0, mirror=False, vflip=True, fit="cover"):
    """Quantize a COLOR image to the panel's 4-color palette; index == 2-bit code."""
    from PIL import Image
    im = Image.open(path).convert("RGB")
    if rotate:
        im = im.rotate(-rotate, expand=True)
    if mirror:
        im = im.transpose(Image.FLIP_LEFT_RIGHT)
    im = _fit(im, fit)
    if vflip:
        im = im.transpose(Image.FLIP_TOP_BOTTOM)
    # Snap near-white / low-saturation-bright pixels to pure white so warm cream
    # backgrounds don't dither into speckled yellow. Keep saturated colors.
    arr = np.asarray(im).astype(int)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    lum = (r + g + b) / 3
    sat = arr.max(2) - arr.min(2)
    white_mask = (lum > 200) & (sat < 40)
    arr[white_mask] = [255, 255, 255]
    im = Image.fromarray(arr.astype(np.uint8), "RGB")
    pal = Image.new("P", (1, 1))
    flat = []
    for c in range(4):
        flat += list(PANEL_COLORS[c])
    flat += [0, 0, 0] * (256 - 4)
    pal.putpalette(flat)
    q = im.quantize(palette=pal, dither=Image.FLOYDSTEINBERG)  # indices 0..3 == codes
    return _pack_codes(np.asarray(q))


CODE_BLACK, CODE_WHITE = 0, 1


def encode_image(path, rotate=0, mirror=False, vflip=True, fit="cover", mode="bw"):
    """Convert any image to a 400x300 2bpp e-ink framebuffer.

    Packing: row-major, 4 px/byte, MSB-first, 2 bits/pixel (confirmed from capture).
    Panel is black/white/red; `mode="bw"` dithers to black+white (matches the app).
    Confirmed on-device defaults: fit="cover", vflip=True.
    """
    from PIL import Image
    im = Image.open(path).convert("L")
    if rotate:
        im = im.rotate(-rotate, expand=True)  # negative = clockwise
    if mirror:
        im = im.transpose(Image.FLIP_LEFT_RIGHT)
    im = _fit(im, fit)
    if vflip:
        im = im.transpose(Image.FLIP_TOP_BOTTOM)

    # 1-bit Floyd-Steinberg dither (mode "1"): 0=black, 255=white
    bw = np.asarray(im.convert("1"))
    codes = np.where(bw, CODE_WHITE, CODE_BLACK).astype(np.uint8).reshape(-1)

    out = bytearray()
    for i in range(0, len(codes), 4):
        out.append((codes[i] << 6) | (codes[i+1] << 4) | (codes[i+2] << 2) | codes[i+3])
    return bytes(out)


async def cmd_dispatch(args):
    client = await connect(args.addr)
    try:
        pp = PicPak(client)
        await pp.subscribe()
        slots = await pp.call(FF01, 0x34, b"\x02")
        total, used, free, bits, nxt = parse_slotmap(slots)
        print(f"slots: total={total} used={used} free={free} bitmap=0x{bits:x} next_free={nxt}")
        slot = args.slot if args.slot is not None else nxt

        if args.raw:
            fb = open(args.image, "rb").read()
        elif args.bw:
            fb = encode_image(args.image, rotate=args.rotate,
                              mirror=args.mirror, fit=args.fit)
        else:
            fb = encode_color(args.image, rotate=args.rotate,
                              mirror=args.mirror, fit=args.fit)
        if args.save and not args.raw:
            dump("last_encoded_fb.bin", fb)  # opt-in debug copy of the rendering
        ok = await send_framebuffer(pp, fb, slot)
        # verify
        slots2 = await pp.call(FF01, 0x34, b"\x02")
        t2, u2, f2, b2, n2 = parse_slotmap(slots2)
        print(f"after: used={u2} bitmap=0x{b2:x}  {'OK slot now occupied' if (b2>>(slot-1))&1 else 'slot NOT occupied?!'}")
    finally:
        await client.disconnect()


async def cmd_calib(args):
    """Send several candidate encodings of one image to consecutive free slots,
    in a single connection, so the user can pick the best on-device."""
    candidates = [
        ("contain-vflip", dict(fit="contain", vflip=True)),
        ("cover-vflip",   dict(fit="cover",   vflip=True)),
        ("stretch-vflip", dict(fit="stretch", vflip=True)),
        ("contain-noflip", dict(fit="contain", vflip=False)),
    ]
    client = await connect(args.addr)
    try:
        pp = PicPak(client)
        await pp.subscribe()
        slots = await pp.call(FF01, 0x34, b"\x02")
        total, used, free, bits, nxt = parse_slotmap(slots)
        print(f"slots used={used}, starting at free slot {nxt}")
        slot = nxt
        for name, kw in candidates:
            fb = encode_image(args.image, **kw)
            dump(f"cand_{name}.bin", fb)
            print(f"\n-> slot {slot}: {name}")
            await send_framebuffer(pp, fb, slot)
            slot += 1
        print(f"\nSent {len(candidates)} candidates to slots {nxt}..{slot-1}:")
        for i, (name, _) in enumerate(candidates):
            print(f"   slot {nxt+i}: {name}")
        print("Cycle through them on the device and tell me which slot looks correct.")
    finally:
        await client.disconnect()


async def cmd_ccalib(args):
    """Send a 4-color palette strip + a color-encoded image, one connection."""
    client = await connect(args.addr)
    try:
        pp = PicPak(client)
        await pp.subscribe()
        slots = await pp.call(FF01, 0x34, b"\x02")
        _, used, _, bits, nxt = parse_slotmap(slots)
        slot = nxt
        print(f"palette strip -> slot {slot} (stripes L->R = code 0,1,2,3)")
        await send_framebuffer(pp, make_palette_strips(), slot)
        slot += 1
        print(f"color image -> slot {slot}")
        fb = encode_color(args.image, fit="cover", vflip=True)
        dump("color_encoded.bin", fb)
        await send_framebuffer(pp, fb, slot)
        print(f"\nSlot {nxt}: palette strips (tell me the 4 colors left->right).")
        print(f"Slot {nxt+1}: color Snorlax (tell me if colors look right).")
    finally:
        await client.disconnect()


def build_argparser():
    ap = argparse.ArgumentParser(description="PicPak BLE client")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan"); s.add_argument("--timeout", type=float, default=12.0)
    s.set_defaults(func=cmd_scan)
    p = sub.add_parser("probe"); p.add_argument("addr")
    p.set_defaults(func=cmd_probe)
    d = sub.add_parser("dispatch")
    d.add_argument("addr")
    d.add_argument("image", help="image file, or a 30000-byte .bin with --raw")
    d.add_argument("slot", nargs="?", type=int, default=None)
    d.add_argument("--raw", action="store_true", help="image is a raw 30000B framebuffer")
    d.add_argument("--rotate", type=int, default=0)
    d.add_argument("--mirror", action="store_true")
    d.add_argument("--fit", default="cover", choices=["cover", "contain", "stretch"])
    d.add_argument("--bw", action="store_true", help="black/white only (no red/yellow)")
    d.add_argument("--save", action="store_true",
                   help="also write the encoded framebuffer to captures/ (debug)")
    d.set_defaults(func=cmd_dispatch)
    c = sub.add_parser("calib")
    c.add_argument("addr")
    c.add_argument("image")
    c.set_defaults(func=cmd_calib)
    cc = sub.add_parser("ccalib")
    cc.add_argument("addr")
    cc.add_argument("image")
    cc.set_defaults(func=cmd_ccalib)
    return ap


def main():
    ap = build_argparser()
    args = ap.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
