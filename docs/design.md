# Design and requirements

## Purpose and observed baseline

Provide a small local camera console with independent live streams and camera
controls, followed by bounded recording and verified delivery to configurable storage.
It must work without Frigate or a vendor cloud account. All project text is English.

The running prototype uses Python 3.11 standard-library HTTPS, vanilla HTML/CSS/JS,
hls.js and FFmpeg. Node.js is not a runtime dependency. The web service supports
independent configured camera workers, local application accounts and per-camera
grants. Recording remains planned.

The user accepted the current viewer and PTZ. A left-button web click physically
moved and stopped the camera. Other directions need individual observation. The
one-segment HLS target measured 4.2–4.4 seconds behind the playlist edge over 65
seconds; this excludes delay before HLS. The UI reports that distinction. Source
keyframes currently produce four-second segments.

Both observed RTSP profiles emit 15 fps: main 1920×1080 and substream 640×360. ONVIF
reports a 30 fps encoder limit, but a reversible 15/30 encoder write, readback and
reconnect experiment left the FHD stream at 15 fps. The original encoder configuration
was restored and two fresh segments verified at 15 fps. No evidence supports 60 fps.
The viewer reports measured source FPS and does not offer an unverified FPS selector.

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
does not stop other workers. Keep the camera cards and controls independent so several
live streams remain visible together. Collapsing PTZ or Settings must not stop a camera
stream. Polling status must not create extra RTSP sessions.

Each camera card has one stateful Start/Stop toggle, a quality selector, compact PTZ
controls and its own movement-duration slider. One global toggle starts all stopped
cameras or stops active cameras. A separate bulk-quality action applies only to
authorized cameras, resolves each camera's supported profile and reports per-camera
results. Never silently substitute a different resolution. Serialize bulk startup with
configurable bounded concurrency to avoid reconnect storms.

Start/stop and quality affect the camera worker shared by authorized viewers, as in
the prototype. Show that scope in the UI. Start/stop live viewing must not stop a
recorder. Unsupported PTZ controls are disabled. Keep movement duration 0.2–1.0 s,
step 0.1, default 0.8, with a Stop in the failure path.

## Persistent startup preferences

The browser currently saves its Full HD autostart choice in local storage. The choice
is local to that browser and does not change camera config. Server-wide desired state,
startup retries and per-camera saved quality remain future work. Autostart defaults
off. The 120-second reaper applies only to viewer workers and must not stop recording.

## Users, access rules and tunnel boundaries

Separate web accounts from device credentials. The service stores salted scrypt
password hashes, roles and grants in a private SQLite file and keeps camera secrets in
protected external files. HTTPS Basic authentication is checked on every page, API
and media request; invalid attempts are rate limited. Disabling an account blocks its
next request. The service does not issue cookies or sessions.
Do not send camera credentials to the browser or share one device password among users.

The implemented roles are `web_admin`, `camera_admin` and `viewer`. A web
administrator manages accounts, camera grants and allowed source addresses. A camera
administrator can control assigned cameras; a viewer can only watch assigned cameras.
The initial web administrator is bootstrapped from the default camera login. The
account database is mode 0600 under the service user's state directory. New account
passwords require 12 characters.

Maintain a global source-IP/CIDR allowlist with optional per-user restrictions.
The operator maintains the initial private address inventory in ignored local
configuration. Do not copy private addresses into tracked files.
An allowed IP still requires authentication. Changes prevent disabling or downgrading
the last enabled administrator and removing the current administrator's last allowed
source network. Local CLI recovery and an audit trail remain open work.

Direct requests use the socket peer address. Ignore forwarded identity/IP headers
unless the peer is an explicitly trusted proxy. For a future reverse proxy, enforce
an exact public origin and validate its forwarded-address chain. Tailscale/WireGuard
supply reachability, not automatic application administrator rights. Cloudflare
Access headers become identity only through a configured validation adapter.
Prepare configuration boundaries now; do not deploy or expose a tunnel in this slice.
Inspect existing private ingress before selecting a tunnel route or hostname.

## Frontend direction

Keep vanilla HTML/CSS/JS with local hls.js. Use compact independent camera cards,
clear active quality, per-camera errors and an in-card PTZ toggle with a centered
direction pad and short movement slider. A left menu switches Home and Settings;
Security / Access contains separate Access and Users tabs. Show source FPS and HLS
player delay separately. Preserve keyboard/focus access and avoid a Node server solely
for styling. Browser state changes must ignore stale responses and release superseded
HLS players/timers.

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

Continuous recording must be independent of viewers, selected quality and login
sessions. The current slice only captures user-requested FHD clips. It opens a separate
RTSP session and copies video/audio into a Matroska file without transcoding. Before
adding a continuous lightweight stream, prove simultaneous main/substream sessions
and the camera's session limits.
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

Manual FHD recording writes to a private spool, verifies each segment with ffprobe,
computes SHA-256 and stores metadata beside the file. The configurable spool defaults
to 2 GiB with a 5 GiB filesystem free-space reserve; sample config defaults recording
off. Startup removes incomplete temporary output and orphaned media. One recording
session is allowed at a time. Each session is split into clips no longer than 15
minutes; the default automatic stop is one hour. A per-session custom cap is accepted
up to 12 hours and is not saved in server config. The quota and free-space reserve can
stop a session early. Camera administrators can stop sessions and delete saved clips;
the service has no automatic retention policy. The UI provides authenticated
downloads. Continuous recording and its oldest-first disposable ring remain future
work.

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
2. Independent multi-camera viewing: the backend and compact grid support independent
   configured workers, per-camera controls, and bulk start/stop and quality. Acceptance
   still needs two real cameras; one failing camera must leave the other usable.
3. Application access: account creation, camera grants, source restrictions, account
   disable and media authorization have unit coverage. Browser acceptance and local
   administrator recovery remain outstanding.
4. Startup and frontend: the browser can save local Full HD autostart. Acceptance still
   needs restart behavior, partial bulk failures and live-lag observation with multiple
   players.
5. Local recording: FHD sessions can be started/stopped, split into 15-minute clips,
   downloaded and deleted from a camera tile, within configured spool/free-space limits
   and with verified metadata. Automated tests cover session limits, permissions and
   quota behavior. A real 2.9-second stop produced a 1920×1080/15 fps clip; its
   downloaded SHA-256 matched metadata and the clip was deleted. Continuous ring,
   schedule/event triggers and session resume after restart remain future work.
6. Real storage delivery: verify identity, permissions, interruption/resume, promotion
   and acknowledgement with the storage owner after that destination is ready.

Each item includes only the relevant automated checks plus real browser/device
observations. Reuse passing evidence while its code/environment remains unchanged.
Keep one implementation slice active; close or explicitly block it before expanding.
Feature commits/pushes are authorized. PRs and merges need separate authorization.
