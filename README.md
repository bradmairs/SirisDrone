# SirisDrone Studio

A self-hosted web app for editing drone photos and videos. You crop, colour
grade and stamp each shot with the **BRM Highlife** logo, then export at full
resolution straight to a folder on your NAS.

It runs in a browser on your computer, tablet or phone. The processing happens
on the machine running the app, ideally the NAS itself.

## Features

| | |
|---|---|
| **Upload / import** | Drag & drop photos and videos, or *Import from NAS* to edit footage already on the NAS without uploading it again. Supports JPEG, DNG (RAW), PNG, TIFF, MP4, MOV and MKV, including 4K 10-bit H.265. |
| **Crop & rotate** | Drag-to-crop with a rule-of-thirds grid. Aspect presets: 16:9, 9:16 (Reels/TikTok), 4:5 (Instagram), 1:1, 4:3, 3:2, 2.39:1, 21:9, original, free. Rotate 90°, flip, and **straighten** (horizon levelling) with automatic zoom so no empty corners show. |
| **Colour grade** | Every new upload starts with **BRM Blue**, the BRM Highlife house look: deep, clear sky and ocean blues, while greens, sand and rock stay natural. On top of that you get exposure, contrast, highlights, shadows, whites, blacks, **dehaze** (cuts through aerial haze), fade, temperature, tint, vibrance, saturation, split toning, and **Blues** saturation/brightness controls that only affect sky and water. |
| **Presets** | BRM Blue, Natural, Vivid Aerial, Clear Skies, Golden Hour, Teal & Orange, Moody, Cinematic Fade, Tropical, Black & White, D-Log M quick fix. Each one shows a live thumbnail of your shot. Save your own looks, and use *Set as default* to change what new uploads start with. |
| **LUTs** | Upload `.cube` LUTs, e.g. DJI's official D-Log M → Rec.709 LUT, with a strength slider. |
| **Detail** | Sharpen and vignette. |
| **BRM Highlife stamp** | On by default and placed like your posts: black script, bottom-right, about a quarter of the image's short edge wide, with a small margin. Adjust size, margin, opacity, drop shadow, colour (white / black / gold / any) and position, e.g. switch to white on dark shots. You can swap in another logo (a transparent PNG works best). |
| **Video** | Scrub through the clip, trim in/out, change speed (0.25×–4× for slow-mo or hyperlapse), remove audio, and save any frame as a full-resolution photo. |
| **Batch** | Copy and paste settings, apply settings to many selected items, set a default look for new uploads, and export many items at once. |
| **Export** | Full resolution or resized. JPEG (quality slider), 16-bit TIFF or PNG for photos. H.264, H.265 10-bit or ProRes 422 HQ for video. Keeps camera EXIF (with an option to **remove GPS**), never overwrites unless asked, and writes files atomically so a half-finished export never appears on the NAS. |
| **Live preview** | Before/after comparison (hold **B**) and a histogram. The preview uses exactly the same colour pipeline as the export, so what you see is what you get. |

## Running it

### Option A – Docker on the NAS (recommended)

This works with Synology Container Manager, QNAP Container Station, Unraid,
TrueNAS and any other system with Docker.

1. Copy this folder onto the NAS.
2. Edit `docker-compose.yml` and set the left-hand side of the volume lines to
   your real shares:
   ```yaml
   volumes:
     - ./data:/data
     - /volume1/Drone/Exports:/nas/exports     # finished files go here
     - /volume1/Drone/Footage:/nas/footage:ro  # optional: import without uploading
   ```
3. Optional but recommended: uncomment `user: "1026:100"` and set it to your NAS
   account's UID:GID (`id <your-user>` over SSH). Exported files then belong to
   you instead of `root`, so you can rename or delete them from your PC.
4. Run `docker compose up -d --build`. On Synology you can also create a
   *Project* in Container Manager from this folder.
5. Open `http://<nas-ip>:8080` in a browser.

### Option B – on a PC with the NAS share mounted

You need Python 3.10+ and FFmpeg (`winget install ffmpeg`, `brew install ffmpeg`
or `apt install ffmpeg`).

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Point the export folder at your NAS share:
export SIRIS_OUTPUT_ROOTS="NAS=/Volumes/Drone/Exports"            # macOS
# export SIRIS_OUTPUT_ROOTS="NAS=/mnt/nas/Drone/Exports"          # Linux (mounted SMB/NFS)
# set SIRIS_OUTPUT_ROOTS=NAS=\\MYNAS\Drone\Exports                # Windows (UNC path works)

python -m app --port 8080
```

Then open <http://localhost:8080>.

### Configuration

| Variable | Meaning | Default |
|---|---|---|
| `SIRIS_OUTPUT_ROOTS` | Export destinations, `Name=path;Name2=path2`. The export dialog lets you browse and create sub-folders inside these. | `./data/exports` |
| `SIRIS_INPUT_ROOTS` | Folders you can *Import from NAS* (read-only is fine). | none |
| `SIRIS_DATA_DIR` | Library, edits, LUTs and settings. | `./data` |
| `SIRIS_USER`, `SIRIS_PASSWORD` | Turn on a password prompt for the web UI. | off |

The app can only read and write inside these folders.

## Using it

1. **Upload** (or drag files onto the window), or **Import from NAS**.
2. Pick a preset, then fine-tune with the sliders. Double-click any slider to
   reset it.
3. Press **C** (or *Crop*) to crop or straighten. Pick an aspect ratio, drag the
   box, then press **Esc**.
4. Check the stamp in the *BRM Highlife Stamp* section.
5. Click **Export…** and choose the NAS folder (e.g. `2026/Beach shoot`), the
   format and quality. The *Exports* panel shows progress.

**Batch editing:** tick several thumbnails (or Ctrl/Shift-click). Then choose
*Apply settings…* to copy the current look to all of them, and *Export… →
Selected*.

**Shortcuts:** `←`/`→` previous/next · hold `B` before/after · `C` crop ·
`I`/`O` set video trim in/out · `Esc` leave crop.

**D-Log / HLG footage:** upload your drone maker's official Rec.709 LUT in
*Colour → LUT*, set it on one clip, then *Apply settings…* to the rest.

## How it works

- `app/grading.py` turns every colour adjustment into a single 3D LUT. Photos,
  the live preview, and video (through FFmpeg's `lut3d` filter) all use that
  same LUT.
- `app/render.py` is the still pipeline: geometry → resize → colour → vignette
  → sharpen → stamp. It works in float, so 16-bit sources such as DNG keep
  their precision.
- `app/video.py` builds an FFmpeg filter graph that mirrors those steps one for
  one. A test checks that the photo and video pipelines produce the same image.
- `app/jobs.py` runs exports one at a time in the background and writes each
  file to a temporary name before renaming it into place.
- `app/static/` is the browser UI: plain HTML, CSS and JS with no build step.

To rebuild the stamp from the original artwork, run
`python tools/make_stamp.py app/assets/brm_highlife_source.jpg`.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```
