# Watchpost web viewer

Watchpost serves the page, APIs and media from one HTTPS port. It checks the
account, camera grants and source network on every request. Camera credentials stay
on the server.

## Prepare the host

`scripts/setup.py` (see the README) does the steps below for you. To do them by hand,
complete the camera setup in the [README](../README.md) first, then:

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

While no account exists, the first sign-in that matches the username and password of
any configured camera creates the root web administrator. Use the same pair the camera
uses for RTSP, usually `admin` and the device password set in the Yoosee app. The
camera must also answer an ONVIF request with that pair, so a stale credentials file
cannot create an administrator. After one account exists this path is closed for good.

The database stores only a salted scrypt hash. Once the administrator exists, sign-in
never contacts a camera, so it works while cameras are offline.

The link happens once. The web password can be changed in Settings without touching
the camera. To also repair cameras whose password changed, tick "Use this password to
repair cameras that reject theirs" on the account: at sign-in the password is tried,
in memory only, on cameras that rejected their saved one. The box is off by default.
Watchpost shows a warning when a web password equals a camera password.

On a host with no reachable camera yet, create the first administrator from the shell
instead:

```sh
./scripts/yoosee-web --config config/local/cameras.toml users add admin --preset admin
```

### When a camera password changes

A camera that answers with an authentication error is marked "rejected the saved
credentials" on its own card; other cameras are unaffected. Web administrators get a
dialog to enter the new username and password. Watchpost tests them on the camera
before saving, can apply them to every camera that failed, and writes them to
`camera-credentials.json` next to the account database (mode 0600). That file
overrides the env file for the cameras it names; the env file is never rewritten.
"Do not ask again for this failure" hides the dialog until the camera fails again.
Offline cameras never trigger it.

New account passwords must have 12 to 256 characters. Create separate accounts
instead of sharing the administrator login.

## Accounts and permissions

Web administrators manage accounts in Settings, or from the shell while the service
runs (changes apply on the next request):

```sh
yw="./scripts/yoosee-web --config config/local/cameras.toml"
$yw users list
$yw users add alice --preset viewer --cameras cam01,cam02
$yw users add door --preset watcher --cameras all
$yw users add ops --preset camera-admin --cameras cam01
$yw users add root2 --preset admin
$yw users passwd alice                 # asks twice; or --password-stdin
$yw users disable alice                # ends her sessions and open streams
$yw users grant alice cam03
$yw users set-networks alice 192.0.2.0/24
$yw networks list
```

| Preset | Can do |
| --- | --- |
| admin | Everything, including accounts and the global network allowlist |
| camera-admin | On granted cameras: start, stop, change profile, PTZ, record, manage clips |
| viewer | On granted cameras: watch, start an idle stream, record, manage own clips |
| watcher | Like viewer, for an unattended screen; records only when allowed |

A viewer starts an idle stream on the camera's default profile (`default_profile` in
the camera section, else the profile with the most pixels). It cannot switch the
profile or stop a stream others are watching. An idle stream stops after about two
minutes without media requests.

Recording is a separate permission, on by default except for watchers. Passwords for
shell-created accounts need 12 to 256 characters. A lost administrator password is
reset with `users passwd NAME` from the host account.

### Watcher accounts and the watch screen

A watcher account is for a screen that stays on. Open `https://SERVER:PORT/watch`: it
starts every camera the account can see, plays them full screen with the same
low-delay player as the main page, and reconnects by itself, with growing delays,
when a camera, the network or the service restarts. The cameras fill the browser
window without scrolling and follow window resizes and full screen (F11). Any account
can open it. Watcher sessions are stored in the
account database, last 90 days from the last sign-in, and survive a restart. Each
watcher account keeps at most two sessions; a new sign-in replaces the oldest.

Other accounts' sessions live in memory and end on restart. They expire after 1 hour
without activity or 3 hours after sign-in.

### Source networks

`web.allowed_networks` is the global allowlist. Each account can also have its own
networks, which must fall inside the global list. An account without its own networks
can sign in from any globally allowed address.

The configuration file is the baseline. At startup and on reload, networks from
`web.allowed_networks` are added to the database marked as coming from the file, and
file entries you removed are dropped. Networks added in Settings or with
`yoosee-web networks add` stay until you remove them there. Settings shows the origin
of each entry and does not remove file entries. Per-account networks are optional.

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

## Reload without restarting

`systemctl --user reload yoosee-cam-web.service` (or `kill -HUP`) re-reads the
configuration: source networks, allowed origins, trusted proxies and the camera list.
Added cameras appear, removed cameras stop (a camera with an active recording stays
until the next reload), and unchanged cameras keep playing for their viewers. A file
with an error is rejected and the service keeps its running configuration; check the
journal. Other settings, such as the port or TLS files, need a restart.

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
browsers except watcher sessions.
