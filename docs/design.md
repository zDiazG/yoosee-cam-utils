# Design status

## Scope

- `yoosee-cam`: ONVIF device and profile inspection, RTSP probing, live viewing,
  short recordings and bounded PTZ commands for one camera at a time.
- `yoosee-web` (Watchpost): HTTPS viewer for several cameras with local accounts,
  per-camera grants, live video and audio, PTZ and manual FHD clips.

[Architecture](architecture.md) describes how the parts fit together and
[Security model](SECURITY.md) describes the trust boundaries.

## Decisions

- **Standard library and FFmpeg only.** No Python dependencies to install or update.
  The cost is hand-written HTTP routing and SOAP handling, kept small and tested.
- **Copy video, encode only audio.** The host does not decode or re-encode video, so
  CPU use stays low. Browsers must support the camera's H.264 stream.
- **One live worker per camera, shared by all viewers.** This limits RTSP sessions on
  the camera. A profile change affects every viewer of that camera.
- **Recording uses its own RTSP session and short verified segments.** A failed
  segment loses at most one segment, and every saved clip is probed and hashed.
- **Credentials stay on the server.** FFmpeg gets them through stdin and logs are
  redacted.

## Constraints

- Profiles and reported frame rates depend on firmware. ONVIF metadata is not proof
  of the delivered frame rate; Watchpost measures it from live segments.
- Recording adds an RTSP session alongside live viewing. Check the camera's session
  limit before raising concurrency.
- HLS latency depends on keyframe spacing. Browsers with MediaSource use fragmented
  MP4 for lower latency.
- Multi-camera behavior is covered by tests with fake cameras. It has not been
  accepted with two physical cameras.

## Known gaps

- Non-watcher sessions are kept in memory and end on restart.
- Configuration reload does not change the port, TLS files or recording spool path.
- A camera credential saved after a password change is stored in clear text, in a
  0600 file.
- Multi-camera behavior is tested with fake cameras only.

## Planned work

In rough order:

1. Acceptance with two physical cameras, including partial failures and concurrent
   recording.
2. A durable clip index and incremental spool accounting for large spools.
3. Scheduled or event-triggered recording with defined retention and recovery.
4. Remote clip copies, once destination identity, integrity checks and retries are
   defined and tested.
