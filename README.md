# Camera utilities

Small Python CLI for ONVIF device/profile inspection, RTSP video and audio checks,
short recordings without transcoding, and bounded PTZ commands.

Requires Python 3.11+, FFmpeg and ffprobe. Run `./install.sh --check`.
Copy `config/cameras.example.toml` to `config/local/cameras.toml`. Put the named
credential variables in the referenced mode-0600 env file outside the repository.
The env file uses literal `NAME=value` lines, without shell expansion or quotes.

```sh
./scripts/yoosee-cam --dry-run device
./scripts/yoosee-cam device
./scripts/yoosee-cam profiles
./scripts/yoosee-cam probe --profile PROFILE_TOKEN
./scripts/yoosee-cam view --profile PROFILE_TOKEN
mkdir -p recordings
./scripts/yoosee-cam record --profile PROFILE_TOKEN --seconds 10 --output recordings/sample.mkv
./scripts/yoosee-cam ptz-move --profile PROFILE_TOKEN --pan 0.1 --seconds 0.2
```

`view` opens ffplay in the current graphical session. `stream` writes live Matroska
video/audio to stdout, suitable for an authenticated SSH pipe to another desktop:

```sh
ssh YOUR_HOST 'cd /path/to/checkout && ./scripts/yoosee-cam stream --profile PROFILE_TOKEN' | ffplay -i -
```

Use `stream --seconds 10` for a bounded check. Stop live viewing with Ctrl+C or close
the player. CLI commands do not start a network listener.

PTZ movement is a dry run unless `--execute` is added. Movement requests include
a timeout and are followed by Stop, with zero-velocity fallback for limited firmware.
A successful response confirms protocol acceptance; it does not independently prove
physical movement. `ptz-status` is optional and may be unsupported.

Camera service paths are configurable. Advertised stream hosts are replaced by the
configured host to handle stale firmware addresses. Set `rtsp_transport = "udp"`
when a camera rejects TCP interleaving. Actual stream metadata can differ from ONVIF.

FFmpeg credentials travel through a stdin playlist instead of process arguments.
Captured errors redact the configured password. Media and private configuration
are ignored by Git. No cloud upload, firmware change or continuous recorder is installed.

An optional [Watchpost HTTPS viewer](docs/web-viewer.md) provides server authentication,
video/audio, profile selection and short PTZ movements with an adjustable pulse
duration. It uses cookie sessions, fragmented MP4 over authenticated HTTPS and the
browser's MediaSource API, with HLS as a compatibility fallback. One camera connection
copies video and encodes audio once for both outputs. It stops after two minutes without media
requests. All viewers share the selected profile.

The viewer now has independent configured camera workers, per-camera controls, bulk
start/stop and quality actions, a saved browser preference to start Full HD streams,
and local viewer accounts with combined roles, camera grants and source-IP
restrictions. Users can receive web-administrator and camera-administrator access
together. The first web administrator uses the default camera login. When manual
recording is enabled in the local config, users with camera access can record FHD
clips. Segments are copied into MP4 without video transcoding, with a default one-hour
session limit and a maximum of 12 hours. A bounded private spool stops recording when
its quota or free-space reserve is reached. The Media page plays clips in the browser.
Owners can delete their own clips; camera and web administrators can hide or delete
clips from other users. Camera tiles show the ten latest visible clips from the last
24 hours. Remote storage replication, continuous recording, automatic retention and
delivery, and a real second-camera test remain outstanding.

Earlier checks reported 15 fps on both profiles. A later timestamp count measured
10.0 fps on FHD and 10.1 fps on the lightweight profile despite the stream reporting
15 fps. A reversible ONVIF write and reconnect test failed to produce 30 fps. The saved
encoder configuration was restored. No 60 fps support was found. MediaSource playback
shows browser frame throughput separately from the source-reported rate.
See [the viewer notes](docs/web-viewer.md) and [design](docs/design.md) for evidence
and remaining acceptance work.

Static CSS and JavaScript use browser caching with ETag revalidation, independently
of login. Camera data, APIs, pages and media stay authenticated and uncached. The
managed Linux service limits allocator arenas to reduce retained authentication
workspace memory. Video decoding and clip playback run in the browser.

Normal browser sessions expire after one hour idle or three hours total. A separately
granted watcher capability permits unattended monitoring. The named private Tailscale
Service is advertised through the existing loopback-only Caddy backend on OPS1. Its
login page and unauthenticated gate are verified over the tailnet. See
[viewer operations](docs/web-viewer.md) for login and deployment behavior.

Run local checks with `PYTHONPATH=src python3 -m unittest discover -s tests`.
