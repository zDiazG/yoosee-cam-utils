# Yoosee camera utilities

Local tools for inspecting ONVIF cameras, checking RTSP streams, recording short
clips and sending bounded PTZ commands. The optional Watchpost web viewer adds
authenticated live viewing and manual FHD recording.

## Requirements

- Python 3.11 or newer
- FFmpeg and ffprobe

Check the local dependencies:

```sh
./install.sh --check
```

## Configure a camera

Copy `config/cameras.example.toml` to `config/local/cameras.toml`. Set the camera
host and credential variable names there. Put those variables in the env file named
by `credentials_file`; keep it outside the repository with mode `0600`. Use literal
`NAME=value` lines without quotes or shell expansion.

Inspect the camera and available profiles:

```sh
./scripts/yoosee-cam doctor
./scripts/yoosee-cam device
./scripts/yoosee-cam profiles
```

## Use the CLI

```sh
./scripts/yoosee-cam probe --profile PROFILE_TOKEN
./scripts/yoosee-cam view --profile PROFILE_TOKEN
./scripts/yoosee-cam stream --profile PROFILE_TOKEN --seconds 10
mkdir -p recordings
./scripts/yoosee-cam record --profile PROFILE_TOKEN --seconds 10 --output recordings/sample.mkv
./scripts/yoosee-cam ptz-move --profile PROFILE_TOKEN --pan 0.1 --seconds 0.2 --execute
```

`view` opens ffplay. `stream` writes Matroska to stdout and can feed a player over
SSH. PTZ commands are dry runs unless `--execute` is present. The camera must
support the requested operation and profile.

## Run the web viewer

The optional HTTPS viewer needs a TLS certificate, local hls.js assets and a
configured `[web]` section. See [viewer setup and operations](docs/web-viewer.md).
Manual recording is disabled until `[recording].enabled` is set to `true`.

## Project notes

[Design and implementation status](docs/design.md) describes the current components,
known constraints and unimplemented work. Keep local configuration, credentials,
clips and runtime data outside tracked source files.
