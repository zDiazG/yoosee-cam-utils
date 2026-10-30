# Design and requirements

## Purpose and observed baseline

Provide a small local camera console with independent live streams and camera
controls, followed by bounded recording and verified delivery to configurable storage.
It must work without Frigate or a vendor cloud account. All project text is English.

The running prototype uses Python 3.11 standard-library HTTPS, vanilla HTML/CSS/JS,
hls.js and FFmpeg. Node.js is not a runtime dependency. The web service supports
independent configured camera workers, local application accounts, per-camera grants
and bounded manual recording.

The user accepted the current viewer and PTZ. A left-button web click physically
moved and stopped the camera. Other directions need individual observation. The
current default uses fragmented MP4 with MediaSource over the existing authenticated
HTTPS listener. Video is copied, audio is converted to AAC, and fragments are flushed
every 200 ms without waiting for each camera keyframe. One RTSP connection also
produces the HLS compatibility fallback. A bounded RAM cache supports new subscribers;
slow subscribers disconnect instead of building an unbounded queue. Active connections
recheck account, camera and source grants every two seconds. The browser follows the
received edge and removes old buffered media. Its buffered-duration label excludes
camera and network delay. HLS still depends on the camera's four-second keyframes.

Earlier checks reported 15 fps on main 1920×1080 and substream 640×360. ONVIF
reports a 30 fps encoder limit, but a reversible 15/30 encoder write, readback and
reconnect experiment left the FHD stream at 15 fps. The original encoder configuration
was restored and two fresh segments verified at 15 fps. No evidence supports 60 fps.
Later packet/timestamp counts measured 10.0 and 10.1 fps respectively, matching about
10 fps decoded by the browser without dropped frames. The cause of the camera's frame
cadence change is unverified. The viewer separates measured browser throughput from
source-reported FPS and does not offer an unverified FPS selector.

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

Accounts can combine the `web_admin`, `camera_admin` and `viewer` roles. A web
administrator manages accounts and has access to every configured camera. A camera
administrator can control assigned cameras; viewers can watch and record clips on
assigned cameras but cannot use PTZ or alter camera stream settings.
Camera IDs accept configured names or an unambiguous numeric suffix, and `ALL` expands
to the configured cameras. The compact multi-select dropdown shows ALL when every
server-exposed role is selected.
The initial web administrator is bootstrapped from the default camera login. The
account database is mode 0600 under the service user's state directory. New account
passwords require 12 to 256 characters. Account and source-network inputs are bounded
and validated server-side; values remain data and are never interpolated into SQL or
HTML. User-management errors return specific validation messages in the viewer.

Maintain a global source-IP/CIDR allowlist with optional per-user restrictions.
The operator maintains the initial private address inventory in ignored local
configuration. Do not copy private addresses into tracked files.
An allowed IP still requires authentication. Changes prevent disabling or downgrading
the last enabled administrator and removing the current administrator's last allowed
source network. Local CLI recovery and an audit trail remain open work.

Direct requests use the socket peer address. Ignore forwarded identity/IP headers
unless the peer is an explicitly trusted proxy. For a future reverse proxy, enforce
an exact public origin and validate its forwarded-address chain. private overlay network/WireGuard
supply reachability, not automatic application administrator rights. Cloudflare
Access headers become identity only through a configured validation adapter.
The next private-ingress batch must preserve source restrictions through an explicitly
trusted proxy and retain application authentication. Inspect the existing private
ingress and Service allowance before provisioning.

## Planned Watchpost access and sessions

Watchpost is the selected name for the web application. Branding and the private
service hostname are pending implementation; existing CLI/package names remain valid.
Use a named private private network service behind the established ingress when available.
Keep deployment addresses and credentials in private configuration. Validate upstream
TLS with the service certificate, stream responses without proxy buffering, and trust
forwarded client addresses only from configured proxy peers. The proxy must sanitize
incoming forwarding headers; direct callers cannot choose their source address.
private overlay network reachability does not grant application permissions.

Replace browser Basic authentication with a login page and opaque server-side session
tokens in Secure, HttpOnly, SameSite cookies. Keep password verification at login,
rate-limit failed logins, validate mutation origins/CSRF tokens, and support logout.
Existing account hashes and camera grants must survive the migration. Account disable,
password changes and access revocation must terminate relevant sessions and streams.

Default accounts expire after 60 minutes without human interaction or after three
hours from login, whichever comes first. Media downloads and background polling do
not extend idle time. A bounded heartbeat reports recent input and page visibility;
server time controls expiration. A resumed or suspended page checks its session before
reconnecting and prompts for authentication when the session is expired or uncertain.

A separately granted `watcher` account capability permits unattended monitoring
without idle or absolute timeout. It grants no camera or administrator access and is
not implicitly selected by All roles. Administrators can revoke it. Logout, password
changes, account disable and service restart still invalidate the session. A browser
restart requires login; cross-restart session persistence is outside this batch.

Acceptance includes accelerated expiry checks, watcher monitoring, logout and live
revocation, preserved accounts, rejected forged forwarding headers, and authenticated
live/PTZ/clip playback through the private URL. Deployment backs up the account store
and ingress configuration; rollback restores them and invalidates issued sessions.

## Resource use and planned reductions

The managed glibc service limits allocator arenas to two and maps allocations above
1 MiB so large scrypt workspaces are released after request threads finish. CSS/JavaScript have public ETag revalidation and
can stay cached after login expires. These files contain no camera or account data;
private APIs and media remain authenticated and uncached.

Video decoding and saved-clip playback already run in the client browser. Keep source
video copying and one shared camera ingest for live viewers. Password hashing on every
HTTP request and duplicate AAC encoding for live/HLS are avoidable server work.
Measure before and after removing repeated password verification and consolidating
AAC encoding or making the HLS compatibility output demand-driven. Preserve the
working MediaSource transport and browser fallback. Do not move camera credentials
or direct RTSP access into browsers. Validate CPU, memory, startup, buffered media and
revocation with one viewer and multiple viewers before claiming a reduction.

## Frontend direction

Keep vanilla HTML/CSS/JS with local hls.js. Use compact independent camera cards,
clear active quality, per-camera errors and an in-card PTZ toggle with a centered
direction pad and short movement slider. A left menu switches Home and Settings, with
a separate Media page for clip playback and management. Security / Access contains
separate Access and Users tabs. Show source FPS and HLS
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
RTSP session and copies video/audio into a browser-compatible MP4 file without
transcoding. Before
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

Manual FHD recording writes to a private spool, copies video into MP4 without
transcoding, verifies each segment with ffprobe, computes SHA-256 and stores metadata
beside the file. Each segment gets a random UUID stored in its filename and sidecar;
deletion does not rewind an allocator, and independent writers do not need to
coordinate counters or maintain an ID database. The safe display filename contains
camera, weekday, timestamp
and resolution. Near-identical overlapping clips receive their own short UUID suffix
in the UI, computed from sidecar metadata without persistent duplicate records. The
configurable spool defaults to 2 GiB with a
5 GiB filesystem free-space reserve; sample config defaults recording off. Startup
removes incomplete temporary output and orphaned media. Sessions are isolated by user
and camera, with one active session per user/camera and a configurable global
concurrency cap. Each session is split into clips no longer than 15
minutes; the default automatic stop is one hour. A per-session custom cap is accepted
up to 12 hours and is not saved in server config. The quota and free-space reserve can
stop a session early. Owners can stop their sessions and delete their clips. Camera
administrators can hide or delete clips for cameras they administer; web administrators
can do so globally. The Media page uses browser playback and HTTP range reads rather
than server-side playback transcoding. Per-camera recent lists cover the previous
24 hours and cap at ten clips. The current filesystem spool is the only active store.
Stable storage metadata prepares for a future verified remote replica, but no remote
transfer is implemented. Continuous recording and its oldest-first disposable ring
remain future work.

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
5. Local recording: users can record clips under their own account; the browser plays
   them with byte-range requests. Per-user/camera session isolation, bounded
   concurrency, metadata, hidden-clip authorization and local spool limits have
   automated coverage. A real 2.9-second clip was previously captured and its SHA-256
   matched metadata. Real browser playback of this new MP4 path and simultaneous
   recordings still need runtime acceptance. Continuous ring, schedule/event triggers,
   retention and remote delivery remain future work.
6. Real storage delivery: verify identity, permissions, interruption/resume, promotion
   and acknowledgement with the storage owner after that destination is ready.

Each item includes only the relevant automated checks plus real browser/device
observations. Reuse passing evidence while its code/environment remains unchanged.
Keep one implementation slice active; close or explicitly block it before expanding.
Feature commits/pushes are authorized. PRs and merges need separate authorization.
