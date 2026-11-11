# Yoosee camera utilities

Local tools for ONVIF/RTSP cameras such as Yoosee models. The `yoosee-cam` CLI
inspects a camera, probes and records RTSP streams, and sends bounded PTZ moves.
The optional Watchpost web viewer adds HTTPS login, per-camera access control, live
video with audio, PTZ and manual FHD clips for several cameras.

Everything runs on one host with the Python standard library and FFmpeg. Nothing is
sent to a cloud service, and the browser never receives camera credentials.

## Requirements

- Linux with Python 3.11 or newer (uses `tomllib`)
- FFmpeg with `ffprobe`
- OpenSSL, only to create a TLS certificate for the web viewer
- A camera reachable over ONVIF (often port 5000 on Yoosee) and RTSP (554)

| Distribution | Packages | Notes |
| --- | --- | --- |
| Debian 12 or newer | `sudo apt install python3 ffmpeg openssl` | |
| Ubuntu 24.04 or newer | `sudo apt install python3 ffmpeg openssl` | Ubuntu 22.04 ships Python 3.10 and is not supported |
| Arch Linux | `sudo pacman -S python ffmpeg openssl` | |

No virtual environment or `pip install` is needed. Check the host:

```sh
./install.sh --check
```

## Configure a camera

1. Copy the example configuration. `config/local/` is ignored by Git.

   ```sh
   mkdir -p config/local
   cp config/cameras.example.toml config/local/cameras.toml
   ```

2. Set each camera's `host`, ports and the names of its credential variables.
3. Create the credentials file named by `credentials_file`, outside the repository,
   with mode `0600`. Use literal `NAME=value` lines without quotes or shell expansion.
   The values are the camera's ONVIF/RTSP account, usually `admin` and the device
   password set in the Yoosee app.

   ```sh
   install -m 600 /dev/null ~/.config/yoosee-cam-utils.env
   ${EDITOR:-nano} ~/.config/yoosee-cam-utils.env
   ```

   Variables with the same names in the process environment take precedence.

4. Check reachability and list the stream profiles:

   ```sh
   ./scripts/yoosee-cam doctor
   ./scripts/yoosee-cam device
   ./scripts/yoosee-cam profiles
   ```

Use `--camera ID` to target a camera other than `cam01`.

## Use the CLI

```sh
./scripts/yoosee-cam probe --profile PROFILE_TOKEN
./scripts/yoosee-cam view --profile PROFILE_TOKEN
./scripts/yoosee-cam stream --profile PROFILE_TOKEN --seconds 10
mkdir -p recordings
./scripts/yoosee-cam record --profile PROFILE_TOKEN --seconds 10 --output recordings/sample.mkv
./scripts/yoosee-cam ptz-move --profile PROFILE_TOKEN --pan 0.1 --seconds 0.2 --execute
```

`view` opens ffplay. `stream` writes Matroska to stdout, so it can feed a player
over SSH. `record` accepts 1 to 300 seconds. PTZ commands are dry runs unless
`--execute` is present, and every move ends with a Stop request. Credentials go to
FFmpeg through stdin, never through command arguments.

## Run the web viewer

The viewer needs a `[web]` section, a TLS certificate, and a locally cached hls.js
file. [Viewer setup and operations](docs/web-viewer.md) covers first sign-in,
accounts, recording, headless hosts and the user service. Manual recording stays
disabled until `[recording].enabled = true`.

## Documentation

- [Viewer setup and operations](docs/web-viewer.md)
- [Architecture](docs/architecture.md): modules, request flow, workers and stored state
- [Security model](docs/security.md): what the access controls cover, and what they cannot
- [Design status](docs/design.md): implemented scope, constraints and planned work

Keep configuration, credentials, TLS keys, clips and account databases outside
tracked files.

## Tests

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

The tests use fake cameras and do not contact real devices.
