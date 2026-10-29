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

HLS playback targets one four-second segment behind the live edge. The page shows
the player's measured delay from that edge. This does not include delay inside the
camera's RTSP feed. If playback falls behind, hls.js may briefly increase playback
speed up to 1.1×; if lag exceeds three target segments, it seeks back toward the live
target. PTZ requests use the selected movement duration (0.2–1.0 seconds, default
0.8) and an explicit stop. The server rejects values outside the slider range. The
request sends an ONVIF duration such as `PT0.8S`.
This is a timed pulse, not a calibrated angle. The active quality and Stop Video
action are shared by all viewers. A worker is stopped after 120 seconds without
media requests.

## Current limitations

The lightweight profile works on the tested camera. The HLS playlist has four-second
segments because the copied camera stream provides keyframes at that interval. A
headless browser measured 8.2 seconds of latency with the prior two-segment target,
stable over 90 seconds. The deployed one-segment target measured 4.2–4.4 seconds over
65 seconds, stable and without fatal playback errors. This measures player delay from
the HLS edge, not camera-to-server delay. The page now displays this measurement so
the two can be distinguished. A headless browser cannot confirm the perceived camera-
to-screen delay on the user's device.

The web PTZ uses the camera-compatible WS-Addressing SOAP serialization. A physical
browser click moved the camera left and it stopped. The slider sends a duration from
0.2 to 1.0 seconds in 0.1-second steps, defaulting to 0.8 seconds. Other directions
and playback continuity during movement remain unverified.

Live RTSP and HLS segment probes of both profiles report 15 fps: 1920×1080 for the
main stream and 640×360 for the substream. Each four-second FHD segment contains 60
frames. ONVIF reports a 30 fps configured limit, but the emitted stream remains at
15 fps. I tested the saved main encoder configuration with requested limits of 15 and
30, both live and after reconnecting the HLS worker. Readback stayed at 30 and the
stream stayed at 15 in every case. I restored the saved encoder configuration and
verified another two FHD segments at 15 fps. The inspected ONVIF options do not
advertise 60 fps. No FPS selector is exposed because the camera ignored the 30 fps
change; the UI displays measured output FPS instead.

The camera grid has independent configured workers, profile selectors and stream
controls, with bulk start/stop and quality actions. The browser can save a local
preference to start each camera at Full HD when available. This preference lives in
that browser's local storage. A real second camera, separate application accounts,
and per-camera authorization remain unverified or unimplemented.
All project text and UI strings must remain English.

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
