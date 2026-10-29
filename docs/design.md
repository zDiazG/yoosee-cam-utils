# Design and requirements

## Purpose and observed baseline

Provide a small local camera console with independent live streams and camera
controls, followed by bounded recording and verified delivery to configurable storage.
It must work without Frigate or a vendor cloud account. All project text is English.

The running prototype uses Python 3.11 standard-library HTTPS, vanilla HTML/CSS/JS,
hls.js and FFmpeg. Node.js is not a runtime dependency. TOML already accepts named
cameras, but the web process currently selects one camera and authenticates with its
credentials. Multi-camera web control, application users and recording are planned.

The user accepted the current viewer and PTZ. A left-button web click physically
moved and stopped the camera. Other directions need individual observation. The
one-segment HLS target measured 4.2–4.4 seconds behind the playlist edge over 65
seconds; this excludes delay before HLS. The UI reports that distinction. Source
keyframes currently produce four-second segments.

Both observed RTSP profiles emit 15 fps: main 1920×1080 and substream 640×360. ONVIF
advertises up to 30 fps but returns inconsistent encoder metadata. No encoder-write
experiment has yet established 30 fps. No evidence supports 60 fps. Playback-rate
catch-up is not a camera FPS change.

## Independent cameras and a shared control panel

Each camera has a stable ID, display name, configured reachable host/ports, credential
references, transport, profile mapping and capability status. Different routed
subnets and existing tunnels are supported when the server can reach those endpoints.
Adding a camera does not create network routes, VPNs or firewall rules. RTSP endpoints
remain constrained to that configured camera; advertised firmware URLs are untrusted.
An explicit tunnel endpoint may use a different port. Configure transport per camera;
UDP media needs more than an RTSP TCP port forward, so tunnel checks must verify video.

Use a camera registry with one independent worker lifecycle, RPC lock and error
state per camera. Reuse current behavior behind camera-scoped APIs. One unreachable
camera must not block another camera's media, PTZ or startup. A camera selection
changes the shared controls and selected-camera video panel without stopping other
camera workers. A compact grid can keep several live players visible. Detach hidden
players to bound browser decoding; server-side desired state is independent of which
panel is selected. Polling thumbnails must not create extra camera RTSP sessions.

The selected camera gets Start/retry, Stop, quality and bounded PTZ controls. A split
Start button offers Start all and Stop all. A separate bulk-quality action applies
only to authorized cameras, resolves each camera's supported profile and reports
per-camera results. Never silently substitute a different resolution. Serialize
bulk startup with configurable bounded concurrency to avoid reconnect storms.

Start/stop and quality affect the camera worker shared by authorized viewers, as in
the prototype. Show that scope in the UI. Start/stop live viewing must not stop a
recorder. Unsupported PTZ controls are disabled. Keep movement duration 0.2–1.0 s,
step 0.1, default 0.8, with a Stop in the failure path.

## Persistent startup preferences

Store desired live state and per-camera preferred quality atomically in a private
SQLite state file outside Git. Separate immutable/configured camera inventory from
mutable application state. The CLI and admin UI expose the same settings, including
an explicit autostart option. Record a desired quality such as FHD separately from
the device-specific profile token.

Autostart defaults off. When enabled, service startup starts available authorized
cameras at their saved quality, with bounded retries/backoff and visible failures.
A late camera can reconnect without restarting healthy cameras. Stop all persists
stopped desired state so a reload cannot undo the operator action. An explicit
Start all resumes it. Configuration may separately declare startup defaults.
The current 120-second idle reaper must stop only on-demand workers; it must not
silently cancel explicitly persistent live workers or recording. Expose resource
usage and allow autostart to be disabled without deleting camera configuration.

## Users, access rules and tunnel boundaries

Separate web accounts from device credentials. Store salted scrypt password hashes,
roles and grants in private SQLite; keep camera secrets in protected external files.
Use HTTPS sessions with Secure/HttpOnly/SameSite cookies, CSRF protection, server-side
authorization on every API/media request, rate limits and revocable sessions.
Do not send camera credentials to the browser or share one device password among users.

Roles are global administrator, camera administrator scoped to assigned cameras,
operator with view/PTZ grants, and viewer. The account called root is an application
administrator, not an operating-system root login. Existing camera-admin login can
be migrated into a camera-scoped web account without exposing its device secret.
A global administrator manages cameras, users, grants and allowed source addresses.
A camera administrator manages access and restricted mode for assigned cameras only.

Maintain a global source-IP/CIDR allowlist with optional per-user restrictions.
Initial private inventory includes the operator desktop, workstation, operations,
storage and services hosts after their current addresses are resolved and reviewed.
An allowed IP still requires authentication. A global restricted mode revokes normal
sessions and admits only global administrators. Camera restricted mode admits global
administrators and that camera's administrators; other cameras retain their grants.
Changes must prevent accidental removal of the last working administrator access.
Provide local CLI recovery for account/source-policy errors, with an audit trail.

Direct requests use the socket peer address. Ignore forwarded identity/IP headers
unless the peer is an explicitly trusted proxy. For a future reverse proxy, enforce
an exact public origin and validate its forwarded-address chain. private overlay network/WireGuard
supply reachability, not automatic application administrator rights. Cloudflare
Access headers become identity only through a configured validation adapter.
Prepare configuration boundaries now; do not deploy or expose a tunnel in this slice.
Inspect existing private ingress before selecting a tunnel route or hostname.

## Frontend direction

Keep vanilla HTML/CSS/JS with local hls.js. Improve responsive layout, typography,
selected-camera status, compact grid, per-camera errors and clear active quality.
Use one selected-camera control panel and a small administration view. Show requested
versus actual FPS and HLS delay separately. Preserve keyboard/focus access and avoid
extra frameworks or a Node server solely for styling. Browser state changes must
ignore stale responses and release superseded HLS players/timers.

## Source FPS experiment

First snapshot exact encoder/source configuration and profile associations. Compare
FrameRateLimit, EncodingInterval, resolution, bitrate and keyframe settings with a
bounded real-frame measurement. Metadata alone is not acceptance evidence.

Attempt 15 then 30 fps using the supported encoder operation while preserving other
settings. Read back each result, reconnect only the affected stream if necessary,
and count decoded frames across a bounded sample. Record interruption, stability,
resolution and bitrate. Do not assume ForcePersistence=false makes the change
transient; device implementations/specification versions differ. Always restore the
saved configuration explicitly after the experiment and verify restoration.
If restore fails, stop encoder writes and report the exact residual state.

Only expose validated choices per profile. Offer FHD 30 as the preferred default
only if it really produces approximately 30 distinct source frames per second.
Otherwise show 15 fps and the unsupported/ignored 30-fps result clearly. Changing
an encoder can affect all clients using that configuration, even another profile;
identify shared configurations before applying. Do not promise interruption-free FPS
changes or an HD/FHD 30/60 option before device evidence exists.

## Recording, cache and delivery

Recording is independent of viewers, selected quality and application login sessions.
One owner per camera records the lightweight source continuously without transcoding.
First prove simultaneous real main/substream sessions and device session limits.
A gateway relay or shared ingest is justified only when measured session limits or
routing require it. The actual lightweight profile is 640×360 on the current camera;
do not relabel it HD merely because the vendor app does.

Manual, scheduled and authenticated external motion/object events request FHD clips.
A clip records camera ID, UTC interval, trigger, actual profile/FPS, byte size and
SHA-256. On-demand recording cannot recover earlier frames; FHD pre-event buffering
is off initially. Use idempotent event IDs and prevent overlapping duplicate captures.
Schedules survive restart and define timezone/missed-run behavior. External detection
adapters are planned; object detection itself is not part of the first recording slice.
A later local motion adapter needs explicit CPU and false-positive acceptance.

Use RAM for disposable previews and a private SSD spool for pending durable clips.
Initial configurable safeguards are a 2 GiB total spool and a 5 GiB filesystem free-
space reserve, with recording disabled by default. Refuse an incompatible budget.
Manual clips default to 30 seconds and are capped at 300 seconds. Continuous footage
uses bounded segments, initially 60 seconds, and a disposable oldest-first ring.
Protect finalized manual/event clips until delivery. Reserve event capacity within
the total budget. At capacity, expire disposable continuous segments first, then
pause/reject recording explicitly rather than filling disk or silently losing events.
A destination outage necessarily limits how much footage can be retained locally.

Finalize files before enqueueing. SQLite journals queue transitions and recovery.
A local disposable destination can exercise delivery while real storage is unavailable.
The later SSH/SFTP adapter stages at the destination, resumes safely, verifies full
size/SHA, flushes and promotes atomically before acknowledging delivery. Keep source
files until verified acknowledgement. Host identity and key trust must be configured.
A lost acknowledgement retries idempotently; a checksum mismatch never promotes.
Keep destination paths, quotas, retention and SSH identity configurable.

Destination layout separates camera/date/continuous, events and manual clips.
Continuous retention is age/quota based initially. Missing motion events cannot
justify deletion until detection coverage is accepted. No real remote-storage
integration is claimed before its mount, ownership, protocol and recovery contract
are available. Validation uses short clips with an explicit temporary quota and
removes disposable test media after evidence is collected.

## Delivery order and acceptance

1. Source FPS evidence: demonstrate the actual 15/30 result and verified restoration.
2. Independent multi-camera viewing: keep two streams active; selection, stop, quality
   and PTZ affect only their intended camera. One failing camera leaves the other usable.
3. Application access: demonstrate role/grant isolation, source restrictions, session
   revocation, media authorization and administrator recovery with two accounts.
4. Startup and frontend: saved FHD/autostart survives restart; bulk operations report
   partial failures; multiple players remain usable without growing playback lag.
5. Local recording: manual clip, lightweight ring and scheduled/external triggers stay
   within quota through restart and a simulated destination outage. Verify checksums,
   idempotency and removal of disposable test media.
6. Real storage delivery: verify identity, permissions, interruption/resume, promotion
   and acknowledgement with the storage owner after that destination is ready.

Each item includes only the relevant automated checks plus real browser/device
observations. Reuse passing evidence while its code/environment remains unchanged.
Keep one implementation slice active; close or explicitly block it before expanding.
Feature commits/pushes are authorized. PRs and merges need separate authorization.
