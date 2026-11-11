# Watchpost web viewer

Watchpost serves the page, APIs and media from one HTTPS port. It checks the
account, camera grants and source network on every request. Camera credentials stay
on the server.

## Prepare the host

Complete the camera setup in the [README](../README.md) first, then:

1. Add the `[web]` section to `config/local/cameras.toml`. The example binds to
   `127.0.0.1`; change `bind` and `allowed_networks` for the clients that should
   connect, and open the same port in the host firewall only for those clients.
2. Cache the pinned hls.js release. The script checks the archive, script and license
   hashes. Browsers never load hls.js from a CDN.

   ```sh
   python3 scripts/cache-web-assets.py --apply
   ```

3. Create a TLS certificate. A self-signed certificate works on a private network;
   put every name and address that clients use in `subjectAltName`:

   ```sh
   install -d -m 700 ~/.config/yoosee-cam-utils/tls
   openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 825 \
     -subj "/CN=watchpost" -addext "subjectAltName=DNS:watchpost.lan,IP:192.0.2.5" \
     -keyout ~/.config/yoosee-cam-utils/tls/server.key \
     -out ~/.config/yoosee-cam-utils/tls/server.crt
   chmod 600 ~/.config/yoosee-cam-utils/tls/server.key
   ```

   The server refuses to start if the key is readable by group or others.

4. Check the configuration without opening the listener, then start the server:

   ```sh
   ./scripts/yoosee-web --config config/local/cameras.toml --dry-run
   ./scripts/yoosee-web --config config/local/cameras.toml
   ```

On a headless host, confirm the listener with
`curl -k https://127.0.0.1:8780/login`. It should return the sign-in page.

## First sign-in

On first start, when the account database is empty, Watchpost creates one web
administrator from the default camera's credentials: `cam01`, or the first camera in
the file, or the one passed with `--camera`. Sign in with the same username and
password the camera uses for RTSP, which is usually `admin` and the device password
set in the Yoosee app. That password must have at least 8 characters.

The database stores only a salted scrypt hash. Sign-in never contacts a camera, so
the administrator can still sign in while cameras are offline.

This link happens once. Later changes to the camera password do not change the web
password, and the web password can be changed in Settings without touching the
camera. If the camera password changes, update the credentials file and restart the
service so live video and recording can reach the camera again.

New account passwords must have 12 to 256 characters. Create separate accounts
instead of sharing the administrator login.

## Accounts and permissions

Accounts are managed in Settings by a web administrator. There is no command-line
account tool yet.

| Grant | Can do |
| --- | --- |
| Web administrator | Everything, including accounts and the global network allowlist |
| Camera administrator on a camera | View, start and stop streams, change profile, PTZ, record, manage clips of that camera |
| Viewer on a camera | Watch a stream that is already running, record through the API, manage own clips |

A viewer cannot start a stopped stream, and the page disables its record button.
The stream must be started by a camera administrator. An idle stream stops after
about two minutes without media requests.

The **watcher** option marks an unattended display account. Its session has no idle
or absolute timeout and its cookie has no expiry date. Sessions live in server
memory, so a service restart signs every account out, watchers included.

Other sessions expire after 1 hour without activity or 3 hours after sign-in.

### Source networks

`web.allowed_networks` is the global allowlist. Each account can also have its own
networks, which must fall inside the global list. An account without its own networks
can sign in from any globally allowed address.

Networks from the configuration are copied into the account database at startup.
Networks added in Settings live only in the database. Removing a network from the
configuration does not remove it from the database, and new configuration networks
are not added to the first administrator's own list. Check both places after a
change; the tracking issue for this drift is open.

## Live video and PTZ

The viewer prefers fragmented MP4 through the browser MediaSource API. Video is
copied from the camera without re-encoding; audio is encoded as AAC. HLS is the
fallback and has more delay because it depends on camera keyframes. Video starts
muted when the browser requires it for autoplay.

Each camera has its own profile selector, stream controls and PTZ pad. A profile
change applies to the shared camera worker, so every viewer of that camera sees it.
PTZ moves last 0.2 to 1.0 seconds and always end with Stop. Up to 8 browsers can
receive one camera's live stream at the same time.

## Manual recording

Set `[recording].enabled = true` to allow manual FHD clips. A user can run one
session per camera. Sessions have a global concurrency limit, a default one-hour stop
and a 12-hour maximum. Each session is saved as segments of at most 15 minutes. Each
segment opens a new RTSP connection, so there can be short gaps between clips.

The spool enforces a byte quota and a free-space reserve. Each MP4 is checked with
ffprobe and saved with a JSON sidecar: owner, camera, times, media details and
SHA-256. The Media page plays clips with byte-range requests. Owners can delete their
clips; camera and web administrators can hide, restore or delete clips of their
cameras.

Recording uses one more RTSP session than live viewing. Cameras with a low session
limit can drop live video while recording. Automatic retention, continuous recording,
schedules, motion triggers and remote copies are not implemented.

## Behind a reverse proxy

Set `web.trusted_proxies` to the exact proxy address. Only that peer may send
`X-Forwarded-For`, and it must send exactly one client address. If the proxy rewrites
the `Host` header, add the public origin, such as `https://watchpost.example`, to
`web.allowed_origins`. Keep the global allowlist narrow; the proxy does not replace it.

## Install as a user service

The installer prints a dry run by default. `--apply` verifies the unit, then installs
and starts it:

```sh
python3 scripts/install-web-service.py --config config/local/cameras.toml
python3 scripts/install-web-service.py --config config/local/cameras.toml --apply
systemctl --user status yoosee-cam-web.service
journalctl --user -u yoosee-cam-web.service -n 50
```

On a headless host, or to start the service at boot without a login session, enable
lingering for the account once:

```sh
sudo loginctl enable-linger "$USER"
```

The installer refuses to replace a unit it did not write, and keeps a copy of a
managed unit before changing it. It does not touch the firewall, reverse proxy or
system services. The service runs from the current checkout; restart it after
changing configuration or code:

```sh
systemctl --user restart yoosee-cam-web.service
systemctl --user disable --now yoosee-cam-web.service
```

A restart stops active recordings, tries to save each current segment, and signs out
every browser.
