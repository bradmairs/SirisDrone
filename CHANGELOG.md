# Changelog

Versions go up by **0.01** for small fixes and tweaks and by **0.1** for significant
features (see `CLAUDE.md`). Each release is tagged `v<version>` on `main`.

## 1.14 – 2026-10-13

- **Location lookup:** also uses OpenStreetMap's district (e.g. *Summerlands* on the west of
  Phillip Island, which came out as just "Victoria") and, when no town is found, the island
  name before the state.

## 1.13 – 2026-10-13

- **Location for videos without GPS:** when a video has no location in its metadata or a DJI
  `.SRT` file, it uses the GPS of the nearest-numbered photo from the same flight (same folder,
  or `JPEG/`/`DNG/` next to a `Videos/` folder, within 50 file numbers).

## 1.12 – 2026-10-13

- **Location stamp style:** a thin accent bar, the town in bold spaced capitals and the country
  lighter (e.g. "| CAPE WOOLAMAI  AUSTRALIA"), scaled so it looks the same size as before.

## 1.11 – 2026-10-13

- **Location stamp:** a soft halo in the opposite colour (Shadow 30, adjustable) so the thin text
  stays readable over busy backgrounds like surf.
- **Fix:** the library's All / Photos / Videos tabs are no longer squashed in a narrow panel;
  "Select all" moves to its own line instead.

## 1.1 – 2026-10-13

- **Location stamp:** "City, Country" (e.g. "Cowes, Australia") in the bottom-left of photos and
  videos, looked up from the file's GPS (EXIF, video metadata or DJI .SRT) when it is added.
  Smaller than the BRM Highlife stamp, with automatic black/white contrast. Editable in the new
  *Location Stamp* section (text, *From GPS*, size, margin, colour); filled in at export for
  items added before 1.1.
- **Instagram exports removed:** the Instagram post / carousel one-click exports and the
  automation's Instagram options are gone; automation exports photos as full-quality JPEGs.
  Saved automation settings that used Instagram switch to full resolution.
- **Fix:** the library's selection actions wrap instead of spilling into the viewer.

## 1.0 – 2026-10-11

First release of SirisDrone Studio, the BRM Highlife drone photo & video editor.

- **Editor:** upload or import from the NAS; crop with aspect presets, rotate, flip and
  straighten; full colour grade with the **BRM Blue** house look, presets (incl. D-Cinelike
  for the DJI Mini 3 Pro), `.cube` LUTs, Blues controls, split toning, sharpen, vignette and
  a graduated sky filter; before/after, histogram and live preview that matches the export.
- **BRM Highlife stamp:** bottom-right black script by default, automatic black/white
  contrast option, full size/position/colour control and one-click reset.
- **Video:** in-app playback (original and "with edits"), trim, speed, mute, stabilise,
  noise reduction and frame grabs.
- **Exports to the NAS:** one-click Instagram post / carousel, Apple TV 4K (HEVC 10-bit,
  up to 60 fps) and full resolution; custom sizes, file-name patterns with `{date}`,
  `{place}` and `{n}`; EXIF kept with optional GPS removal; atomic writes.
- **Automation:** watch folder that auto-edits (level horizon, exposure, BRM Blue, stamp)
  and exports new Mini 3 Pro files – photos as an Instagram post (keeping the photo's
  orientation) plus a full-quality JPEG, videos for Apple TV 4K – with optional deletion of
  originals once exported.
- **Extras:** HDR bracket merge, hyperlapse from photos, DNG support with a camera-matched look.
- **Performance:** NVIDIA GPU (NVENC/NVDEC) video encoding/decoding with automatic CPU fallback.
- **Deployment:** Docker / docker-compose for the server with GPU access.
