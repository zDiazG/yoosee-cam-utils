# HTTPS viewer

The optional Python server serves video, audio and PTZ over a single HTTPS port.
Authentication is checked by the server for the page, API, playlist and every video
segment. It uses the camera username/password from the private credential file.
No credential is embedded in JavaScript. Invalid logins are rate limited; network
sources must also match the configured allowlist. PTZ POSTs require a custom header
and reject cross-origin browser requests.

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

Open `https://YOUR_SERVER:8780` and use the configured camera credentials at the
browser's authentication prompt. Video starts
muted because browsers restrict autoplay with sound; unmute in the video controls.
Profiles are labeled using the resolutions advertised by the camera, which may
differ from its mobile app's HD/FHD labels. Changing the profile replaces the current
player and starts the selected stream; Start or retry is available if startup fails.

HLS introduces several seconds of video delay. PTZ requests are short movements
with a timeout and an explicit stop; do not repeatedly click while waiting for the
delayed video to catch up. The active quality and Stop Video action are shared by
all viewers. A worker is stopped after 120 seconds without media requests.

## Current limitations

The lightweight profile works on the tested camera. Full HD takes several minutes to
stabilize in the user's browser, but server-side HLS output remained continuous in a
26-second sample; the browser delay's cause is not established. A bounded
WS-Addressing PTZ request moved the camera right and the user confirmed it stopped.
The web API now uses that request format, and its integrated movement call returned
HTTP200 with Stop confirmed. Other directions and playback continuity during movement
remain unverified. No source-FPS setting is currently exposed. All project text and
UI strings must remain English.

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
Restart the user service after changing its private configuration or updating code.

Stop and disable the viewer with:

```sh
systemctl --user disable --now yoosee-cam-web.service
```

The configuration, credentials and certificate are retained. Remove only firewall
rules specifically created for this viewer when retiring it.
