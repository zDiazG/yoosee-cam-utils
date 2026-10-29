# Design and requirements

## Outcome and current state

Provide reliable local viewing and camera control, followed by continuous low-bandwidth
recording and event/manual Full HD clips delivered to a configurable storage host.
The recording path must work without Frigate or a cloud camera account.

The current prototype provides authenticated HTTPS viewing and CLI diagnostics.
Low-bandwidth viewing works on a tested device. The deployed Full HD player measured
4.2–4.4 seconds behind its HLS live edge over a 65-second browser sample. This does
not measure delay inside the camera's RTSP feed or the user's own device. The page
shows HLS-edge delay. A real web PTZ click moved the camera left and the camera
stopped. Other directions and playback continuity during movement are not yet
accepted. FPS configuration, continuous recording and a durable clip-transfer queue
are not implemented. API success and short media samples do not establish regular use.

## Requirements

- Keep UI, source text, logs, documentation and configuration examples in English.
- Replace the old viewing worker when quality changes. Stop the old player, wait
  for worker exit, release its session, and attach only the newly selected stream.
  Report startup failure explicitly rather than silently showing another quality.
- Measure cold start, first decoded frame, stalls and buffer growth. Preserve source
  video where possible. Compare source RTP, HLS segments and browser timing before
  changing transport or adding a media server.
- Validate PTZ by actual movement and bounded stopping, with no associated sustained
  video stalls. An HTTP acknowledgement alone is insufficient.
- Query encoder capabilities and measure delivered FPS per profile. Expose 15/30/60
  only when supported and validated by the device. Do not present duplicated frames
  or browser playback rate as increased camera FPS. Save and restore encoder
  configuration around tests; unsupported settings must be identified clearly.
- Make cache paths, storage destinations, retention, transfer limits and camera
  settings configurable. Keep credentials and host inventory outside Git.
- Keep control and media endpoints authenticated. New recording/trigger APIs must
  preserve server-side authorization and cross-origin protections.

## Recording decisions

These decisions define the intended implementation. They are not deployed features.

### Capture ownership and independent operation

Continuous low-bandwidth capture should run on the storage host when it can reach
the camera reliably. A gateway relay is an alternative when routing requires it.
Choose one owner for a recording, independent of browser viewers and quality changes.
Confirm that the camera supports simultaneous main/substream sessions before relying
on both. Frigate may supply triggers later; it is not a required dependency.

### Full HD event and manual clips

Manual requests and motion/automation events produce identifiable Full HD clips.
Keep capture separate from uploading. A clip includes camera identity, UTC start/end,
trigger type, profile, byte size and checksum. Finalize the local file before transfer.
Pre-event Full HD footage requires a continuously maintained Full HD buffer; it is
off in the first slice. An on-demand clip cannot recover earlier unseen frames.

### Bounded cache and verified delivery

Use RAM for disposable preview buffers. Use a durable local SSD spool and persistent
queue for clips that must survive a restart or storage outage. RAM-only staging
must spill to SSD before the request is considered safely queued.

Transfer over existing authenticated SSH transport to a staging name at the storage
host. Resume safely, verify size and whole-file checksum, flush and promote the file
atomically, then acknowledge delivery. Remove the local source only after that
acknowledgement. Duplicate requests and worker restarts must be idempotent.

Set explicit spool limits and a full-cache policy. A remote outage necessarily
consumes local storage. Stop/reduce disposable continuous capture before discarding
protected clips; when protected capacity is exhausted, reject new captures clearly
and report the condition. Do not silently delete pending uploads.

### Storage layout and retention

Separate continuous segments, motion clips and manual clips by camera, UTC date and
recording identifier. Apply configurable age/quota retention on the destination.
Keep recordings separate from other applications' data and backup trees.
Do not delete continuous footage merely because no detection was reported until
event coverage has been validated. Time-based retention is the initial baseline.

## Protocol investigation decisions

This is a private homelab tool, not a plan to publish a Yoosee replacement. Inspect
public protocol notes and code as useful references, and adapt what is useful for
this private setup. Compatibility still has to be demonstrated against this camera;
another GWIPC model can speak a different protocol.

Prefer bounded local protocol tests. A public GWIPC component sends a vendor TCP
RTSP SET_PARAMETER PTZ request; a separate Yoosee integration uses ONVIF SOAP with
WS-Addressing and the same profile token as this camera. These are concrete leads,
not yet verified against this unit. If they do not explain the working control path,
use the official Android APK listed at https://download.yoosee.com/ without needing
the package installed on the user's phone. Record version and hash, inspect it first,
then use isolated dynamic testing only as needed. The vendor download page also lists
a Windows CMS client. Firmware changes, remote shells and account bypass are outside
this work.

## Acceptance sequence

1. Reliable cold start and automatic profile replacement, measured in the browser.
2. Physically verified PTZ and truthful per-profile FPS controls.
3. One manual Full HD clip survives a destination outage and is delivered with a
   matching checksum, without losing or duplicating the local queued recording.
4. Continuous low-bandwidth capture continues during viewing/profile changes;
   event-triggered Full HD capture and retention work within configured limits.

An isolated local destination can validate queue/failure behavior before the real
storage host is ready. Production storage, routing and filesystem acceptance remain
separate integration work. No merge or deployment is implied by this design.
