# Architecture

The project is a small standard-library Python package plus a static browser app.
FFmpeg does all media work. There is no build step, database server or message
queue.

## Modules

| Path | Responsibility |
| --- | --- |
| `src/yoosee_cam_utils/cli.py` | Configuration and credential loading, ONVIF SOAP client (`Camera`), RTSP input generation, `yoosee-cam` commands |
| `src/yoosee_cam_utils/web.py` | HTTPS server, sessions, accounts (`UserStore`), per-camera live workers (`Streams`, `CameraRegistry`), request routing |
| `src/yoosee_cam_utils/live.py` | `LiveBuffer`: parses FFmpeg's fragmented MP4 output and fans it out to browsers with bounded memory |
| `src/yoosee_cam_utils/recording.py` | `ClipManager`: manual recording sessions, segment checks, spool quota, clip catalog |
| `src/yoosee_cam_utils/static/` | Login page and viewer app (plain JavaScript, no framework) |
| `scripts/` | Launchers, hls.js cache script, user service installer |
| `tests/` | `unittest` suites with fake cameras and fake FFmpeg processes |

## Camera access

`Camera.call` sends ONVIF SOAP requests with a WS-Security password digest. Calls to
one camera are spaced at least 0.4 seconds apart and time out after 6 seconds. The
stream URI returned by the camera is rewritten to the configured host and RTSP port,
because some firmware advertises stale addresses.

FFmpeg never receives credentials in its arguments. `media_input` builds a one-line
`ffconcat` playlist containing the authenticated RTSP URL and writes it to FFmpeg's
stdin. Error output passes through `redact` before it is printed or returned.

## Live viewing

```
camera --RTSP--> ffmpeg (one per camera, copy video, AAC audio)
                   |-- HLS segments in a private cache directory --> /media/...
                   '-- fragmented MP4 on stdout --> LiveBuffer --> /live/... (up to 8 browsers)
```

`CameraRegistry` holds one `Streams` worker per configured camera. A failure or slow
ONVIF call on one camera does not block media for another: each worker has separate
locks for camera RPC, lifecycle and state. A worker stops when no media request has
touched it for about two minutes. Starting a different profile replaces the worker
for every viewer of that camera.

`LiveBuffer` keeps the MP4 initialization segment and at most 12 seconds or 8 MiB of
fragments. A browser that falls behind is disconnected instead of growing memory.
Each long-lived media response re-checks the session and camera grant every 2
seconds, so revocation applies to open streams.

## Recording

Each manual session runs in its own thread and opens its own RTSP connection per
segment:

1. Compute the byte budget from the quota, free-space reserve and active sessions.
2. Run FFmpeg for at most one segment (15 minutes or less) into `<uuid>.part.mp4`.
3. Probe the result with ffprobe and reject empty or truncated output.
4. Hash it, rename it to `<uuid>.mp4`, and write the JSON sidecar atomically with
   `fsync`.

At startup, partial files, logs and media without a matching sidecar are removed.
Clips are stored as `<spool>/<camera>/<year>/<month>/<uuid>.mp4`. The sidecar is the
catalog; there is no separate index.

## Requests and sessions

`ViewerServer` is a `ThreadingHTTPServer` that wraps each connection in TLS 1.2 or
newer. `Handler` routes requests by path:

- `/login`, `/login.js`, `/app.js`, `/style.css`, `/vendor/hls.min.js` are public
  static files with no account data.
- `/api/auth/login` checks origin, rate limit, password and source network, then
  issues a session.
- Every other route requires a valid session from an allowed source address.
  Mutations also need the `X-Camera-Client` header, a matching HTTPS `Origin` and
  the session's CSRF token.

Sessions are random tokens kept in memory as SHA-256 digests, bound to the client
address, and limited to 512 at a time. Each request reloads the account's grants
from SQLite, so permission changes apply on the next request.

## Stored state

| State | Location (default) | Owner |
| --- | --- | --- |
| Camera list, web and recording settings | `config/local/cameras.toml` | Operator, read at startup |
| Camera credentials | file named by `credentials_file`, mode 0600 | Operator |
| Accounts, roles, grants, networks | `~/.local/state/yoosee-cam-utils/viewer-users.sqlite3`, mode 0600 | Web UI |
| Live HLS segments | `~/.cache/yoosee-cam-utils/hls-runtime/` | Live workers, deleted on stop |
| Clips and sidecars | `~/.local/state/yoosee-cam-utils/recordings/` | `ClipManager` |
| TLS certificate and key | `~/.config/yoosee-cam-utils/tls/` | Operator |

The TOML file is read once at startup. `allowed_networks` is merged into SQLite with
insert-only semantics, and the first administrator is created only when the account
table is empty. After that, accounts and networks are managed in SQLite. The
[design notes](design.md#known-gaps) list the drift this causes.
