# Yoosee camera utilities

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
the player. No HTTP listener or unauthenticated camera dashboard is created.

PTZ movement is a dry run unless `--execute` is added. Movement requests include
a timeout and are followed by Stop, with zero-velocity fallback for limited firmware.
A successful response confirms protocol acceptance; it does not independently prove
physical movement. `ptz-status` is optional and may be unsupported.

Camera service paths are configurable. Advertised stream hosts are replaced by the
configured host to handle stale firmware addresses. Set `rtsp_transport = "udp"`
when a camera rejects TCP interleaving. Actual stream metadata can differ from ONVIF.

FFmpeg credentials travel through a stdin playlist instead of process arguments.
Captured errors redact the configured password. Media and private configuration
are ignored by Git. No web service, cloud upload, firmware change or continuous
recorder is installed.

Run local checks with `PYTHONPATH=src python3 -m unittest discover -s tests`.
