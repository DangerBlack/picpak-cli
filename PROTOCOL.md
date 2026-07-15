# PicPak BLE protocol (reverse-engineered)

Reconstructed from `PicPak-v1.1.1` (Flutter app) + a full HCI-snoop capture of one
real "send picture" flow. Device: pocket e-ink album, FW `V1.1.x`.
(Device MAC/serial redacted — see `probe` output for your own unit.)

## Transport

- **BLE GATT.** Service `0xFF00`. Characteristics:
  - `0xFF01` (handle 0x2a) — main command/data channel (app→dev writes, dev→app indications)
  - `0xFF02` (handle 0x2d) — device-info / config channel
  - `0xFF03` (handle 0x30) — (subscribed, little traffic observed)
  - `0xFF04` (handle 0x33) — misc (cmd 0x48)
  - All four have CCCDs; the app enables notify/indicate on each right after connecting.
- **No BLE bonding / no link-layer encryption.** (0 SMP packets, 0 encryption-start
  commands in the capture.) All auth is application-layer. A fresh central can connect.
- MTU negotiated to **517**; app still caps image chunks at 245-byte ATT writes.
- Device→app replies arrive as **ATT Handle Value Indications** (confirmed).

## Frame format

```
AA <cmd> <body...> FF          # 0xAA start, 0xFF end delimiter
```
Response frames echo the same `cmd` (a few use `cmd+1`, see table). Most command
bodies are: `<u16 index-or-arg LE> <op> [data]` where `op` seen as `02`=read/query,
`01`=data/ok in reply, `00`=write.

## Command map (observed)

| cmd  | dir | meaning | example req → resp |
|------|-----|---------|--------------------|
| 0x06 | ff02| device name | `aa0602ff` → `aa0601 06 "PicPak" ff` |
| 0x07 | ff02| time/tz value | `aa0702ff` → `aa0701 80510100 00 ff` (0x00015180=86400) |
| 0x08 | ff02| device info (ASCII) | `aa080201ff` → HW/FW version, serial, MAC (NUL-separated ASCII) |
| 0x09 | ff02| display/caps params | `aa0902ff` → `aa0901 0300 f303 f401 bc02 0202 ff` |
| 0x04 | ff01| **auth key slots** (16-byte AES blocks) | read: `aa04 0300 02 ff` → `aa04 0300 01 <16B> ff`; write: `aa04 0500 00 <16B> ff` → `aa05 0500 00 ff` |
| 0x34 | ff01| query image slots (→0x35) | `aa3402ff` → `aa35 bc02 <used> 00 <used+?> 00 <bitmap...> ff` |
| 0x38 | ff01| status (→0x39) | `aa3802ff` → `aa39 01 040000 ff` |
| 0x48 | ff04| status (→0x49) | `aa48000000ff` → `aa49 00...00 ff` |
| 0x01 | ff01| **image data chunk** | see below |
| 0x36 | ff01| **commit/finish image** (→0x37) | `aa36 0500 ff` → `aa37 0500 00 ff` |

JSON side-channel (on ff02): a framed JSON command was also sent —
`02 00000001 25 {"cmd":"sync_timezone","offset":7200}` (header `02`, u32 seq=1,
u8 len=0x25, then UTF-8 JSON).

## Image write (the "dispatch")

Image is sent as a **raw packed e-ink framebuffer**, NOT a JPEG. The phone decodes,
crops and dithers the photo, then streams the raw buffer. Observed size **30000 bytes**
= 400×300 @ 2 bits/px (4 gray levels).

Data-chunk frame:
```
AA 01 <imgId:u16 LE> <seq:u16 LE> <len:u16 LE> <data[len]> FF
```
- `imgId` = target slot (here `5` = next free slot; slot query showed 4 used before, 5 after).
- `seq` low byte = chunk index (0,1,2,…); the **final** chunk ORs `0x0100` as an
  end-of-image flag (last observed `seq=0x017f` = index 127 + end flag).
- `len` = data bytes in this chunk (236 for full chunks → 245-byte ATT write; 28 for the tail).
- 128 chunks total → 30000 bytes.

### Full flow observed (after connect + enable notifications on FF01–FF04)
1. `0x09` get display params, `0x34` get slot map, `0x08` get device info.
2. JSON `sync_timezone`.
3. **Auth handshake**: repeated `0x04` reads of key indices 1–4 (16-byte blocks,
   constant within the session) interleaved with device name/status reads, then a
   `0x04` **write** of index 5 (a 16-byte value the app computed) → device acks.
4. Pick next free slot (imgId=5) via `0x34`.
5. Stream 128 `0x01` data chunks (imgId=5, seq 0..127, end flag on last).
6. `0x04` write idx 5 again + `0x36` commit → device acks; `0x34` now shows 5 slots used.

## cmd 0x04 = per-slot image digest (RESOLVED — not auth)

`0x04` is **not** authentication. It reads/writes a **16-byte MD5 digest per image slot**:
- read `aa04 <slot:u16> 02 ff` → `aa04 <slot> 01 <md5[16]> ff`
- write `aa04 <slot:u16> 00 <md5[16]> ff` → `aa05 <slot> 00 ff`

The idx1–4 reads in the capture were the digests of the 4 images already on the device
(static because those images don't change — confirmed identical on a fresh connect from
the PC). The single 0x04 write was the app storing `MD5(framebuffer)` for the new slot.
**There is no cryptographic auth and no BLE bonding** — any central can connect and write.

## Panel & framebuffer (RESOLVED on-device)

- 4.2" **BWRY 4-color e-paper**, 400×300, **2 bits/pixel** = 30000-byte framebuffer.
- 2-bit code palette (verified via on-device strip test): **0=black, 1=white, 2=yellow, 3=red**.
- Framebuffer origin is bottom-left → an upright image must be stored **vertically flipped**.
- App fits portrait sources with **cover** (fill + center-crop) and dithers
  (Floyd–Steinberg). Photos come out mostly black+white; red/yellow only where present.

## Dispatch recipe (implemented in tools/picpak.py)

1. Connect (no bonding), enable notify on FF01–FF04.
2. `0x34` query slots → pick lowest free slot from the bitmap.
3. For each 236-byte chunk: `AA 01 <slot:u16> <seq:u16> <len:u16> <data> FF`
   (seq low byte = index; final chunk ORs `0x0100`).
4. `AA 04 <slot:u16> 00 <MD5(framebuffer)[16]> FF`  (digest)
5. `AA 36 <slot:u16> FF`  (commit) → device acks `aa37 <slot> 00 ff`.
