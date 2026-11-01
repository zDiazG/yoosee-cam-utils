# HTTPS viewer

The optional Watchpost service provides authenticated camera viewing, PTZ and
manual FHD recording over HTTPS. It serves the page, APIs and media from one port.
The server checks account, camera grants and source-network rules. Camera secrets
stay on the server.

## Prepare configuration

1. Copy `config/cameras.example.toml` to `config/local/cameras.toml` and configure
   the camera and `[web]` sections.
2. Install the pinned local hls.js asset:

   ```sh
   python3 scripts/cache-web-assets.py --apply
   ```

   The script verifies the release and file hashes. Browsers do not load hls.js
   from a CDN.
3. Configure a TLS certificate and private key. Keep the key outside the
   repository with mode `0600`.
4. Set `web.bind`, `web.allowed_networks` and the host firewall for the clients
   that should reach the service. Use `web.allowed_origins` only for exact HTTPS
   origins required by a trusted reverse proxy.

Print the camera configuration summary, then start the server. `--dry-run` does not
start the listener:

```sh
./scripts/yoosee-web --config config/local/cameras.toml --dry-run
./scripts/yoosee-web --config config/local/cameras.toml
```

Open `https://SERVER:PORT`. The first web administrator is bootstrapped from the
default camera account. Create separate viewer accounts instead of sharing that
login. New account passwords must be 12 to 256 characters. User source networks
and camera grants are managed in Settings.

## Live video and PTZ

The viewer prefers fragmented MP4 through the browser MediaSource API. It copies
H.264 video and encodes audio as AAC. HLS is the compatibility fallback and may
have more delay because it depends on camera keyframes. Video starts muted when
required by browser autoplay policy.

Each configured camera has a profile selector and its own stream and PTZ controls.
Bulk actions operate on all cameras the account can access. Stream profile changes
apply to the shared worker for that camera and affect other viewers. PTZ uses a
timed movement from 0.2 to 1.0 seconds and sends Stop when the movement ends.

## Manual recording

Set `[recording].enabled = true` to allow manual FHD recording. Users with access
to a camera can start and stop their own session. Sessions have a configurable
global concurrency limit, a default one-hour stop and a maximum duration of 12
hours. The service saves segments no longer than 15 minutes. Each segment opens a
separate RTSP connection, so short gaps can occur between clips.

The local spool enforces a byte quota and filesystem free-space reserve. Each MP4
is checked with ffprobe and stored with a sidecar containing its owner, camera,
timestamps, media details and SHA-256 digest. The Media page plays clips in the
browser and supports byte-range requests. Owners can delete their clips;
administrators can manage clips within their grants.

The spool is the only recording destination. Automatic retention, continuous
recording, schedules, motion triggers and remote replication are not implemented.
Recording uses an additional RTSP session and can affect live viewing on cameras
with limited session capacity.

## Install as a user service

The installer prints a dry run by default. Add `--apply` to install and enable the
user service after preparing configuration and TLS:

```sh
python3 scripts/install-web-service.py --config config/local/cameras.toml
python3 scripts/install-web-service.py --config config/local/cameras.toml --apply
systemctl --user status yoosee-cam-web.service
```

The installer refuses to replace an unmanaged unit. It does not configure a host
firewall, reverse proxy or system-wide service. A user systemd manager must be
available. To stop and disable the service:

```sh
systemctl --user disable --now yoosee-cam-web.service
```

The service uses the current checkout. Restart it after changing configuration or
updating the code.
