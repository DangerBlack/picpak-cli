# PicPak BLE dispatcher

Send any picture to a PicPak pocket e-ink album directly over Bluetooth LE —
no phone, no cloud. Reverse-engineered from the app; protocol details in
[`PROTOCOL.md`](PROTOCOL.md).

Works with the **4.2" BWRY 4-color panel (400×300)**. Other panel models would
need the encoder recalibrated (see *Multiple devices* below).

## Setup (one time)

Already done in this repo, but to recreate:
```bash
python3 -m venv venv
./venv/bin/pip install bleak pillow numpy
```

## How to submit a picture

1. **Put the target device in pairing mode**: long-press its button for ~3 seconds.
   (If it's currently connected to the phone app, turn the phone's Bluetooth off first —
   the device only advertises when not connected.)

2. **Find the device** (grab its MAC address):
   ```bash
   ./venv/bin/python tools/picpak.py scan
   ```
   Look for the line marked `*** PICPAK`, e.g. `AA:BB:CC:DD:EE:FF`.

3. **Dispatch the picture**:
   ```bash
   ./venv/bin/python tools/picpak.py dispatch <MAC> path/to/photo.jpg
   ```
   It picks the next free slot, converts the image to the panel's 4-color format,
   streams it, writes the integrity digest, and commits. You'll see the slot map
   update to confirm success.

> Tip: the advertising window is short. If `scan`/`dispatch` says *"not found"*,
> long-press the button again and rerun immediately.

### Options
```
dispatch <MAC> <image> [SLOT]     # send to a specific slot instead of next-free
  --bw                            # black & white only (no red/yellow)
  --fit cover|contain|stretch     # cover (default) fills+crops; contain letterboxes
  --rotate 90|180|270             # rotate before fitting
  --raw                           # <image> is a ready 30000-byte framebuffer
```

Examples:
```bash
# fill the screen (default), color
./venv/bin/python tools/picpak.py dispatch AA:BB:CC:DD:EE:FF sunset.jpg

# show the whole image with white borders, no cropping
./venv/bin/python tools/picpak.py dispatch AA:BB:CC:DD:EE:FF poster.png --fit contain

# monochrome
./venv/bin/python tools/picpak.py dispatch AA:BB:CC:DD:EE:FF scan.jpg --bw
```

## Multiple devices

Devices are targeted purely by **MAC address**, and there's no pairing secret —
so this works with any number of units of the same model:

```bash
./venv/bin/python tools/picpak.py scan            # lists every PicPak in range
./venv/bin/python tools/picpak.py dispatch AA:BB:CC:DD:EE:FF photo.jpg
```

Put one device in pairing mode at a time, scan, dispatch to its MAC. Nothing is
tied to a specific unit.

**Different panel model?** The transport is generic, but the image encoder assumes
this panel's format (400×300, colors `0=black 1=white 2=yellow 3=red`, cover+vflip).
If a new device has a different resolution the tool will *error* (not corrupt the
screen). To support it, recalibrate with the helper commands:
```bash
./venv/bin/python tools/picpak.py probe  <MAC>        # device info + slot map
./venv/bin/python tools/picpak.py calib  <MAC> img.jpg  # orientation/fit candidates
./venv/bin/python tools/picpak.py ccalib <MAC> img.jpg  # 4-color palette strip + color test
```
