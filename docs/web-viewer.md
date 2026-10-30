# HTTPS viewer

The optional Python server serves video, audio and PTZ over a single HTTPS port.
Authentication is checked by the server for the page, API and media. Long-lived live
connections recheck account status, camera access and source restrictions every two
seconds. The initial administrator uses the camera login from the private credential file.
No credential is embedded in JavaScript. Invalid logins are rate limited; network
sources must also match the configured allowlist. PTZ POSTs require a custom header
and reject cross-origin browser requests.

CSS and JavaScript are public application assets with ETag revalidation. Browsers
may retain them across login expiry; cached code grants no access to APIs or media.
Pages, APIs and media remain authenticated with `Cache-Control: no-store`.

## Preparation

1. Run `python3 scripts/cache-web-assets.py --apply`. This caches hls.js 1.7.4,
   verifies the release/archive and extracted JavaScript hashes, and retains its
   Apache-2.0 license. The browser loads this local copy without a CDN request.
2. Configure the `[web]` section from `config/cameras.example.toml` in your ignored
   operator configuration. Bind only to the intended interface and allow explicit
   client networks. Restrict the matching port in the host firewall too.
3. Supply a TLS certificate matching the server address. Keep the private key mode
   0600 outside the repository. With a self-signed certificate, verify/trust that
   certificate on clients before login. Do not replace HTTPS with plain HTTP.
4. Prefer a private runtime directory on tmpfs for HLS segments. Only a short rolling
   segment window is retained; this viewer is not a recording archive.

```sh
./scripts/yoosee-web --config config/local/cameras.toml --dry-run
./scripts/yoosee-web --config config/local/cameras.toml
```

Open `https://YOUR_SERVER:8780` and use the configured default camera credentials at
the browser's authentication prompt on first run. That account bootstraps the first
web administrator. The administrator can add web administrators, camera-scoped
administrators and viewers, combine these roles on one account, edit grants, set or
reset passwords, restrict each user to source networks, disable accounts and manage
the viewer's global source allowlist. Web administrators also have view and control
access to every configured camera. Camera-administrator and viewer grants can be
combined and scoped per camera. Roles use a compact multi-select dropdown. ALL selects
every role currently exposed by the server and remains the collapsed dropdown label.
Password hashes are stored in the private SQLite database configured by
`auth_database`; new or reset passwords need 12 to 256 characters. The database file
is created with mode 0600. New accounts require at least one source IP or CIDR.
Comma-separated, semicolon-separated and whitespace-separated lists are accepted for
source networks and camera IDs. A numeric suffix such as `02` resolves to the
configured camera ID `cam02`; `ALL` selects every configured camera. View-only and
control camera grants are entered separately. The server validates each value,
limits each list to 64 entries, uses parameterized database statements and returns
field-level validation messages. User-entered values are rendered as text, never HTML.
Video starts muted because browsers restrict autoplay with sound; unmute in the video
controls. Profiles are labeled using advertised resolutions, which may
differ from the mobile app's HD/FHD labels. Each camera card has a single stream toggle,
profile selector and centered PTZ toggle with its own movement duration. The global
stream toggle starts all stopped cameras or stops all active cameras; with a mixed
selection it stops the active streams. Bulk quality actions switch all cameras to Full
HD when available or their lightest profile. The hamburger menu switches between Home
and Settings. The media icon below it opens the clip library. Security / Access has
separate Access and Users tabs and is shown only to web administrators. The browser
can save a preference to start Full HD streams when the
page opens. An allowed source IP still needs a valid
account. The application allowlist does not change the host firewall; update that
separately when a new subnet must reach the service. The server checks the TCP peer
address. Source NAT therefore makes several clients appear as their gateway and removes
per-client IP isolation; preserve source addresses or use a separately trusted ingress
before relying on per-user IP restrictions. Forwarded headers are not trusted.
Default-route CIDRs are rejected.

Live playback prefers fragmented MP4 through MediaSource in compatible browsers.
FFmpeg copies H.264 video and converts audio to AAC, flushing fragments every 200 ms
over authenticated HTTPS. This uses the existing port and requires no extra service,
WebSocket listener or WebRTC media port. Each camera has one RTSP ingest shared by
its live subscribers and HLS output. The server retains at most 8 MiB and 12 seconds
of fragments per camera, permits eight live clients per camera, and disconnects slow
clients when they fall behind the cache. The browser follows about 0.3–0.8 seconds
behind received media and trims old buffered data. The label reports this buffer,
not total camera-to-screen latency. Pausing suspends catch-up until playback resumes.

Browsers without the required MediaSource codec support use HLS. That fallback still
depends on camera keyframe spacing and cannot promise one-second latency. Its buffer
is bounded and stalls do not keep increasing its synchronization target.
PTZ requests use the selected movement duration (0.2–1.0 seconds, default
0.8) and an explicit stop. The server rejects values outside the slider range. The
request sends an ONVIF duration such as `PT0.8S`.
This is a timed pulse, not a calibrated angle. Stream controls affect the shared
worker for that camera, so all viewers of that camera see a profile change. Each
worker stops after 120 seconds without media requests.

## Recording sessions

Recording is enabled only when `[recording].enabled = true` in the private operator
config. Users with a camera viewing grant can start and stop their own FHD session from
its camera card. Sessions are scoped to the user and camera. One user cannot start a
second session for the same camera while their first is active; different users and
cameras do not share a global recording lock. The configured default permits four
concurrent sessions across the service to bound disk and camera load.
The default auto-stop limit is one hour; presets include 15 and 30 minutes. A custom
limit applies only to the current browser tab and is capped at 12 hours. The server
validates that cap on every request. It saves separate clips no longer than 15
minutes, using a new RTSP connection for each segment. The reconnect can create a
short gap between clips. Stopping early finalizes the current segment. The spool has a
configured byte limit and free-space reserve; when either is reached, the session
stops with a status message.

Each saved segment is copied into MP4 without video transcoding, verified with ffprobe
and has a sidecar with camera ID, owner, UUID, UTC start/end, dimensions, measured FPS,
duration, size and SHA-256. The UUID is the permanent clip identity in the filename
and sidecar. It is generated independently for every capture, so deletion does not
rewind an allocator and multiple storage writers need no shared counter or ID
database. The browser localizes the weekday/date label; the display title uses
camera ID, weekday, local timestamp to seconds and FHD/HD/SD. If clips are near
duplicates (same camera/resolution, start and end within 30 seconds, duration within
a 2:1 ratio, and at least 80% overlap), the UI appends each clip's own short UUID
prefix to distinguish them. This is computed from sidecars at listing time.
Startup removes incomplete temporary files and media without a valid sidecar.

The Media page plays clips in the client's browser with HTTP byte-range requests. The
server serves file bytes and does not decode or transcode them for playback. New clips
support seeking in browsers with H.264/AAC MP4 support. Older Matroska clips remain
available through the same route, though browser codec support varies. The camera
cards show up to ten visible clips from the previous 24 hours. The Media page lists
all visible clips for cameras the account can view. Owners can delete their own clips.
Camera administrators can hide or delete clips from cameras they administer; web
administrators can do so for every camera. Hidden clips are omitted from normal lists
and playback and appear only in the administrators' Hidden view.

The active store is the local private spool. Clip metadata records a stable clip ID,
storage backend and replication state so a future remote delivery adapter can copy
verified files and keep the local copy until remote acknowledgement. No remote
replication or fallback transfer is active yet. There is no automatic retention
policy, 24/7 ring, motion trigger or schedule in this release. Each recording adds an
RTSP session and may affect live viewing on cameras with a low session limit; verify
simultaneous load before enabling continuous capture.

## Current limitations

The current FHD MediaSource path played at 1920x1080 for a 30-second browser sample
with 0.65–0.72 seconds buffered, no dropped frames and no browser errors. The camera
OSD clock was approximately 1–2 seconds behind the capture time, with one-second OSD
precision and no independently verified camera-clock offset. This does not establish
an absolute end-to-end latency guarantee. The ingest probe delivered fragments at a
median interval of 0.197 seconds and a maximum interval of 0.267 seconds. Longer runs,
concurrent cameras, remote clients and the lightweight MediaSource profile need their
own acceptance evidence. HLS remains a compatibility fallback with longer latency.

The web PTZ uses the camera-compatible WS-Addressing SOAP serialization. A physical
browser click moved the camera left and it stopped. The slider sends a duration from
0.2 to 1.0 seconds in 0.1-second steps, defaulting to 0.8 seconds. Other directions
and playback continuity during movement remain unverified.

Live RTSP and HLS segment metadata of both profiles report 15 fps: 1920×1080 for the
main stream and 640×360 for the substream. Each four-second FHD segment contains 60
frames. ONVIF reports a 30 fps configured limit, but the emitted stream remains at
15 fps. I tested the saved main encoder configuration with requested limits of 15 and
30, both live and after reconnecting the HLS worker. Readback stayed at 30 and the
stream stayed at 15 in every case. I restored the saved encoder configuration and
verified another two FHD segments at 15 fps. The inspected ONVIF options do not
advertise 60 fps. No FPS selector is exposed because the camera ignored the 30 fps
change. A later packet/timestamp count measured 10.0 fps on FHD over 9.3 seconds and
10.1 fps on the lightweight profile over 9.7 seconds, with a median 0.1-second frame
interval. The browser decoded about 10 fps without dropped frames. These observations
show that metadata can overstate the delivered rate; they do not identify the camera's
reason for changing frame cadence. The MediaSource UI therefore displays browser frame
throughput separately from the source-reported rate. Changing playback transport does
not create additional camera frames.

The camera grid has independent configured workers, profile selectors and stream
toggles, compact per-camera PTZ and recording controls, and bulk start/stop and quality
actions. The browser can save a local
preference to start each camera at Full HD when available. This preference lives in
that browser's local storage. Account grants and source restrictions have unit
coverage, but a real second camera remains untested. The source allowlist applies at
the application; the host firewall remains a separate filter. Forwarded client-IP
headers are ignored, so a tunnel or reverse proxy needs an explicit trusted-proxy
design before it can preserve per-user source restrictions.
All project text and UI strings must remain English.

## Planned private access and sessions

The next batch will use Watchpost branding and a named private service URL. Cookie
sessions, a 60-minute idle limit, a three-hour normal session cap and an explicitly
granted watcher capability are specified in [the design](design.md). The current
runtime still uses Basic authentication and has no application session timeout.

## User service

```sh
python3 scripts/install-web-service.py --config config/local/cameras.toml
python3 scripts/install-web-service.py --config config/local/cameras.toml --apply
systemctl --user status yoosee-cam-web.service
```

The first command is a dry run. The installer refuses to replace an unmanaged unit.
The unit runs as the current user, with a private umask and a scoped runtime
directory. It requires that user's systemd manager; enable lingering separately if
the service must start without an interactive login. No system-wide firewall,
network, reverse-proxy or authentication configuration is edited by the installer.
The managed unit sets `MALLOC_ARENA_MAX=2` and `MALLOC_MMAP_THRESHOLD_=1048576`
on glibc systems. It bounds allocator arenas and releases large password-hashing
workspaces through mapped allocations, without weakening password hashing. Other
allocators may ignore these environment settings.
Restart the user service after changing its private configuration or updating code.

Stop and disable the viewer with:

```sh
systemctl --user disable --now yoosee-cam-web.service
```

The configuration, credentials and certificate are retained. Remove only firewall
rules specifically created for this viewer when retiring it.
