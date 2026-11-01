# Design and implementation status

## Components

- `yoosee-cam`: ONVIF discovery, profile inspection, RTSP probing, live viewing,
  short recordings and PTZ commands.
- `yoosee-web`: optional HTTPS viewer with local accounts, per-camera access,
  live video and audio, PTZ, and manual FHD clips.
- FFmpeg handles RTSP media. The browser decodes live and recorded video.
- Camera credentials come from an external env file or the process environment.
  Web account data and recordings use private local storage.

The web server keeps one live worker per configured camera. A worker produces HLS
and fragmented MP4 from one RTSP connection. It copies video and encodes audio to
AAC. Slow live clients are disconnected when they exceed the bounded fragment
cache. Switching a camera profile affects every authorized viewer using that
camera worker.

Manual recording uses a separate RTSP connection. It writes FHD segments of at
most 15 minutes, copies video, encodes audio for MP4, verifies output with ffprobe
and records a SHA-256 digest in a sidecar. The local spool enforces a byte quota
and free-space reserve. Continuous recording, schedules, motion triggers and
remote replication are not implemented.

## Access model

The viewer uses HTTPS, server-side cookie sessions and local accounts. Accounts
can have viewer, camera-administrator and web-administrator roles. Camera grants
and source-network restrictions are checked by the server. Camera credentials
never go to the browser. The application allowlist does not replace host firewall
rules.

## Constraints

- Camera profiles and reported frame rates depend on firmware. ONVIF metadata is
  not proof of delivered frame rate.
- A camera recording session adds RTSP load alongside live viewing. Check the
  camera's session limit before increasing concurrent use.
- HLS latency depends on camera keyframe spacing. Compatible browsers use
  fragmented MP4 for lower-latency live playback.
- A real second-camera acceptance run and remote recording storage have not been
  completed.

## Planned work

- Verify multi-camera operation with two physical cameras, including partial
  failures and simultaneous recording.
- Add scheduled or event-triggered recording only with defined recovery and
  retention behavior.
- Add remote clip delivery after destination identity, permissions, integrity
  checks and retry behavior are agreed and tested.
