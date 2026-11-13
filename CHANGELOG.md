# Changelog

Versions go up by **0.01** for small fixes and tweaks and by **0.1** for significant
features (see `CLAUDE.md`). Each release is tagged `v<version>` on `main`.

## 1.57 – 2026-11-13

- **Trip map:** *Map* in the top bar shows every shot with a GPS position on satellite imagery
  (or a street map), coloured by trip. Clips show their flight path from the DJI `.SRT`. Click a
  dot for its thumbnail and *Open in the editor*; click a trip in the list to zoom to it.
  Positions are read once and remembered. The map library (Leaflet 1.9.4) is bundled with the
  app; only the map tiles come from the internet.
- **Notifications:** Automation → *Notify me when a trip is done*. Once a trip is finished
  (nothing still copying or exporting, its highlight reel made) you get one message:
  - what was exported and where it went
  - how many shots were skipped as blurry or duplicate
  - the reel, any errors, and a few thumbnails

  It is sent by ntfy (phone push), a webhook (Discord, Slack or any JSON receiver) and/or email.
  Email uses the `SIRIS_SMTP_*` settings in the server's environment. Watch-folder problems
  (e.g. the NAS isn't mounted) are notified at most once an hour. *Send a test* checks the
  setup.
- **Status page:** *Status* shows free space on each NAS folder and the app's data disk,
  watch-folder health (last check, files waiting, recent errors), notifications, GPU, exports,
  library counts and the FFmpeg version, with any problems listed at the top. The dot on the
  button turns red when something needs attention: an unreachable or nearly full (< 20 GB)
  NAS, a stalled watch folder or failed exports.
- **Flight telemetry:** DJI `.SRT` files (Mini 3 Pro and older formats) are read for position,
  altitude and camera settings. Speed and distance from the start are worked out from the
  positions. This is the groundwork for the flight-data overlay.

## 1.47 – 2026-11-13

- **Highlight reel:** select a trip's clips and choose *Highlight reel…* to get one video of
  their best moments. Every clip is sampled twice a second and scored for sharpness, jerky
  motion and over-fast pans (the first and last second are skipped). The best window of each
  clip comes first, then the next best anywhere, up to 30 s – 2 min. The windows are rendered
  with each clip's own edit (grade, crop, stabilise, denoise) and joined in shooting order with
  crossfades. A title card leads (place and date over a blurred first frame, with the BRM
  Highlife logo), the stamp is added once, and the reel fades to black at the end. Output is
  Apple TV 4K or full resolution, on the GPU when available.
- **Match colours:** select a trip's shots and choose *Match colours* to even out exposure and
  white balance. Each shot is nudged most of the way to the trip's median, through its own
  Exposure, Temperature and Tint sliders, so it stays editable. Photos and videos are matched
  separately, and the changes are capped (±0.6 EV, ±25 temperature, ±12 tint) so a sunset stays
  a sunset.
- **Automation:**
  - *Match colour across each trip* is on by default. Each scan's new files are matched against
    the rest of their trip before export.
  - New *Make a highlight reel for each trip* option (30 s – 2 min). The reel is made once a
    trip's clips have all arrived, into the trip's export folder, and remade when more clips
    arrive. With *Delete originals* on, clips are kept until their reel is done.

## 1.37 – 2026-11-13

- **Smart culling:** photos are checked for blur (how wide the sharpest edges are, so a calm
  sea with a crisp horizon still counts as sharp) and for near-duplicates (shots that look the
  same and were taken within 8 seconds, or with neighbouring file numbers). In each burst the
  crispest, least clipped shot is kept; the rest are marked "duplicate of …", and smeared shots
  "blurry". Trips are judged on their own, so new shots are compared with earlier ones.
- **Rejected tab and badges:** rejected photos are dimmed with a red reason badge and listed
  under a new *Rejected* filter. *Cull photos* re-checks the selection (or everything), and
  *Keep* / *Reject* overrides the verdict for the selected photos.
- **Automation skips rejects:** with *Skip blurry & duplicate photos* on (the default), the
  watch folder culls each scan's photos before exporting. Rejects stay in the library and their
  originals are never deleted, even with *Delete originals* on. *Export… → All* skips them too.

## 1.27 – 2026-11-13

- **Automatic checks on GitHub:** every pull request and every push to `main` runs the full test
  suite (with FFmpeg) and checks the web UI's JavaScript, so a broken change can't slip in
  through the automatic merges.
- **Release tags:** after each merge to `main`, the commit is tagged `v<version>` automatically
  (sessions couldn't push tags, so releases since 1.0 were untagged).

## 1.26 – 2026-10-13

- **Fix:** locations looked up before 1.14 were cached, so places that 1.14 now names (e.g.
  *Summerlands* instead of "Victoria") kept the old answer. Those are looked up again.

## 1.25 – 2026-10-13

- **A folder per trip:** the automation now exports into a folder per trip. Copy a trip folder
  (e.g. `2026-04-03_Philip Island - Australia`, with or without `JPEG/`, `DNG/`, `Videos/`
  inside) into the watch folder and its edits go to the same-named folder in the export folder;
  files dropped in loose are grouped by the day they were taken (`2026-04-03`). DJI reuses file
  numbers across trips, so this also stops names clashing. *A folder per trip* in Automation
  turns it off.

## 1.15 – 2026-10-13

- **Fix:** pressing *Clear finished* in the Exports panel while the watch folder was working made
  finished exports look lost, so their originals were kept with "export didn't finish before
  the app restarted". Cleared jobs are now still known to the watch folder.

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
