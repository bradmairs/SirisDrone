# Changelog

Versions go up by **0.01** for small fixes and tweaks and by **0.1** for significant
features (see `CLAUDE.md`). Each release is tagged `v<version>` on `main`.

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
