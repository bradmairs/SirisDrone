# SirisDrone Studio

A self-hosted web app for editing drone photos and videos. You crop, colour
grade and stamp each shot with the **BRM Highlife** logo, then export at full
resolution straight to a folder on your NAS.

It runs in a browser on your computer, tablet or phone. The processing happens
on the machine running the app, ideally your server, using its **NVIDIA GPU**
(e.g. RTX 3060) for video. It's tuned for the **DJI Mini 3 Pro**.

**The fast path:** turn on **Automation**, then copy a flight's files into the NAS
"Inbox" folder. Photos come out as full-quality JPEGs (BRM Blue, level horizon, the BRM Highlife
stamp and a "City, Country" location stamp). Videos come
out as full-quality 4K HEVC files with BRM Blue and the stamp, ready for your
Apple TV 4K screensaver.

## Features

| | |
|---|---|
| **Upload / import** | Drag & drop photos and videos, or *Import from NAS* to edit footage already on the NAS without uploading it again. Supports JPEG, DNG (RAW), PNG, TIFF, MP4, MOV and MKV, including 4K 10-bit H.265. |
| **Automation (watch folder)** | Watches a NAS folder. Each new photo or video is added to the library, auto-edited (BRM Blue, stamp, level horizon, exposure; the D-Cinelike look for 10-bit Mini 3 Pro clips) and exported. Photos go out as full-resolution JPEGs; videos as Apple TV 4K. Files that are still copying are left until they finish, originals are never changed, and you can tweak and re-export anything later. |
| **Location stamp** | "City, Country" (e.g. *Cowes, Australia*) bottom-left, looked up from the photo/video GPS (EXIF, video metadata or the DJI `.SRT`) via OpenStreetMap when the file is added. Smaller than the BRM Highlife stamp, black or white for contrast; edit the text, size and colour in *Location Stamp*. |
| **One-click exports** | **Apple TV 4K** (3840 × 2160 HEVC 10-bit, maximum quality, source frame rate up to 60 fps, `hvc1`-tagged for Apple devices) and **Full resolution**. |
| **NVIDIA GPU** | Video is decoded (NVDEC) and encoded (NVENC H.264 / HEVC 10-bit) on the GPU, including exports, playback previews and hyperlapses. The top bar shows which GPU is in use. If the GPU is missing or busy, the job is redone on the CPU automatically. The colour-grading and stamp filters always run on the CPU, because FFmpeg has no GPU version of them. |
| **Auto fix** | **Auto-level horizon** finds the horizon and straightens it (and leaves straight-down shots alone). **Auto exposure** brightens or darkens mid-tones without clipping the sky. |
| **Crop & rotate** | Drag-to-crop with a rule-of-thirds grid. Aspect presets: 16:9, 9:16 (Reels/TikTok), 4:5 (Instagram), 1:1, 4:3, 3:2, 2.39:1, 21:9, original, free. Rotate 90°, flip, and **straighten** (horizon levelling) with automatic zoom so no empty corners show. |
| **Colour grade** | Every new upload starts with **BRM Blue**, the BRM Highlife house look: deep, clear sky and ocean blues, while greens, sand and rock stay natural. On top of that you get exposure, contrast, highlights, shadows, whites, blacks, **dehaze** (cuts through aerial haze), fade, temperature, tint, vibrance, saturation, split toning, and **Blues** saturation/brightness controls that only affect sky and water. |
| **Presets** | BRM Blue, Natural, Vivid Aerial, Clear Skies, Golden Hour, Teal & Orange, Moody, Cinematic Fade, Tropical, Black & White, and for Mini 3 Pro **D-Cinelike → BRM Blue** and **D-Cinelike → Natural**. Each one shows a live thumbnail of your shot. Save your own looks, and use *Set as default* to change what new uploads start with. |
| **LUTs** | Upload `.cube` LUTs (e.g. a D-Cinelike → Rec.709 LUT) with a strength slider. |
| **Detail** | Sharpen and vignette. |
| **Sky (graduated filter)** | Darkens or deepens the top of the frame (neutral, deep blue, teal or warm) to rescue bright, washed-out skies. |
| **HDR bracket merge** | Select the 3 or 5 shots of an AEB bracket and choose *Merge HDR*. They're aligned (to remove drone drift) and fused into one 16-bit photo with detail in both sky and shadows, which then appears in the library to edit like any photo. |
| **Hyperlapse** | Select a photo sequence and choose *Hyperlapse…*. Every frame gets one photo's edit, then flicker is removed, the result is stabilised, the stamp is added once, and it's exported as 4K video. |
| **BRM Highlife stamp** | On by default and placed like your posts: black script, bottom-right, about a quarter of the image's short edge wide, with a small margin. Adjust size, margin, opacity, drop shadow, colour (white / black / gold / any) and position, e.g. switch to white on dark shots. **Reset stamp to default** puts it back to black, bottom-right, original size in one click. You can swap in another logo (a transparent PNG works best). |
| **Video** | **Stabilise** (two-pass `vidstab`) and **noise reduction** sliders. **Play clips inside the app**: *Original* plays the clip as shot (H.265 / 10-bit DJI clips get a playable H.264 copy automatically), and *With edits* renders a quick preview with your grade, crop, trim, speed and stamp. You can also scrub, trim in/out, change speed (0.25×–4× for slow-mo or hyperlapse), remove audio, and save any frame as a full-resolution photo. |
| **Batch** | Copy and paste settings, apply settings to many selected items, set a default look for new uploads, and export many items at once. |
| **Export** | Full resolution, a preset size (4K, 2.7K, 1080p, Instagram 1080 × 1350, Reels 1080 × 1920…) or a **custom width × height** box; the crop is fitted inside without stretching, and the dialog shows the exact output size. **File names** follow a pattern such as `{name}_BRM` or `{date} {place} {n}`, where `{place}` is the location the shot was taken (e.g. "Wharariki Beach"), from its GPS. JPEG (quality slider), 16-bit TIFF or PNG for photos. H.264, H.265 10-bit or ProRes 422 HQ for video, with a frame-rate choice. Keeps camera EXIF (with an option to **remove GPS**), never overwrites unless asked, and writes files atomically so a half-finished export never appears on the NAS. |
| **Live preview** | Before/after comparison (hold **B**) and a histogram. The preview uses exactly the same colour pipeline as the export, so what you see is what you get. |

## Running it

### Option A – Docker on your server (recommended)

This works with Unraid, TrueNAS SCALE, Ubuntu/Debian, Synology Container
Manager, QNAP Container Station and any other system with Docker.

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
4. **GPU:** install the NVIDIA driver and the
   [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
   on the host. On Unraid, use the *Nvidia Driver* plugin. The compose file
   already asks for the GPU (the `deploy:` block) with
   `NVIDIA_DRIVER_CAPABILITIES=compute,video,utility`. The `video` capability is
   what makes NVENC/NVDEC available inside the container. Without a GPU, delete
   the `deploy:` block.
5. Run `docker compose up -d --build`.
6. Open `http://<server-ip>:8080`. The top bar should say
   **GPU: NVIDIA GeForce RTX 3060**. If it says *CPU encoding*, hover over it to
   see why.
7. To automate: click **Automation**, choose the folder to watch (e.g.
   `Drone footage` / `Inbox`) and where exports go, tick **Watch for new
   files**, then **Save**.

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
| `SIRIS_USER`, `SIRIS_PASSWORD` | Turn on a password prompt for the web UI. Set this if the app can be reached from outside your home network. | off |
| `SIRIS_GPU` | `off` forces CPU video encoding. | auto |
| `SIRIS_GEOCODER` | `off` disables `{place}` lookups, or a URL for your own Nominatim server. | OpenStreetMap |

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

**Shortcuts:** `←`/`→` previous/next · `Space` play/pause video · hold `B` before/after · `C` crop ·
`I`/`O` set video trim in/out · `Esc` leave crop.

**Mini 3 Pro D-Cinelike footage** is 10-bit and flat. Apply the
**D-Cinelike → BRM Blue** preset (the watch folder does this automatically for
10-bit clips), or upload a `.cube` LUT in *Colour → LUT*.

**Apple TV 4K screensavers:** use *Export… → Apple TV 4K*, or let the watch
folder do it. Files are HEVC 10-bit `hvc1`, 3840 × 2160, at the clip's own frame
rate up to 60 fps (120 fps slow-mo clips are reduced to 60, the most Apple TV
plays). Add them to the album or app your screensaver uses.

**Place names** come from OpenStreetMap's free Nominatim service, at most one
lookup per second, cached per location. For videos the GPS is read from the
DJI `.SRT` file saved next to the clip, so keep those together on the NAS.

## How it works

- `app/grading.py` turns every colour adjustment into a single 3D LUT. Photos,
  the live preview, and video (through FFmpeg's `lut3d` filter) all use that
  same LUT.
- `app/render.py` is the still pipeline: geometry → resize → colour → vignette
  → sharpen → stamp. It works in float, so 16-bit sources such as DNG keep
  their precision.
- `app/video.py` builds an FFmpeg filter graph that mirrors those steps one for
  one. A test checks that the photo and video pipelines produce the same image.
- `app/recipes.py` and `app/analysis.py` hold the one-click recipes and the
  automatic horizon/exposure fixes; `app/watch.py` is the watch folder.
- `app/gpu.py` detects NVENC/NVDEC with real test encodes and picks GPU or CPU
  encoder settings; `app/hdr.py` fuses brackets; `app/geo.py` handles `{place}`.
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
