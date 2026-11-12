#!/usr/bin/env python3
"""Guided first-run setup: credentials, config, TLS, assets, checks, optional service."""
import argparse
import getpass
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def env(name, default=None):
    return os.environ.get(name, default)


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__)
    a = p.add_argument
    a("--camera-host", default=env("YOOSEE_CAMERA_HOST"))
    a("--camera-name", default=env("YOOSEE_CAMERA_NAME", "cam01"))
    a("--onvif-port", type=int, default=int(env("YOOSEE_ONVIF_PORT", 5000)))
    a("--rtsp-port", type=int, default=int(env("YOOSEE_RTSP_PORT", 554)))
    a("--camera-user", default=env("YOOSEE_CAMERA_USER", "admin"))
    a("--password-stdin", action="store_true", help="read the camera password from stdin")
    a("--bind", default=env("YOOSEE_BIND", "127.0.0.1"))
    a("--port", type=int, default=int(env("YOOSEE_PORT", 8780)))
    a("--allowed-network", action="append", default=None)
    a("--san", action="append", default=None, help="TLS subjectAltName, e.g. DNS:host or IP:1.2.3.4")
    a("--config-dir", type=Path, default=Path(env("YOOSEE_CONFIG_DIR", "~/.config/yoosee-cam-utils")))
    a("--state-dir", type=Path, default=Path(env("YOOSEE_STATE_DIR", "~/.local/state/yoosee-cam-utils")))
    a("--repo-config", type=Path, default=Path(env("YOOSEE_REPO_CONFIG", str(ROOT / "config/local/cameras.toml"))))
    a("--credentials-file", type=Path, default=Path(env("YOOSEE_CREDENTIALS_FILE", "~/.config/yoosee-cam-utils.env")))
    a("--skip-tls", action="store_true")
    a("--skip-assets", action="store_true")
    a("--skip-check", action="store_true")
    a("--install-service", action="store_true")
    a("--force", action="store_true")
    a("--dry-run", action="store_true")
    args = p.parse_args(argv)
    if args.allowed_network is None:
        args.allowed_network = (env("YOOSEE_ALLOWED_NETWORKS") or "127.0.0.0/8").split(",")
    if args.san is None:
        args.san = (env("YOOSEE_SAN") or "DNS:localhost,IP:127.0.0.1").split(",")
    for name in ("config_dir", "state_dir", "repo_config", "credentials_file"):
        setattr(args, name, getattr(args, name).expanduser().absolute())
    return args


def say(msg):
    print(msg, flush=True)


def fail(msg):
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(2)


def env_prefix(name):
    return re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def resolve_inputs(a):
    password = env("YOOSEE_CAMERA_PASSWORD")
    if a.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    tty = sys.stdin.isatty()
    missing = []
    if not a.camera_host:
        if tty:
            a.camera_host = input("Camera IP or hostname: ").strip()
        if not a.camera_host:
            missing.append("--camera-host (YOOSEE_CAMERA_HOST)")
    if not password and not a.dry_run:
        if tty and not a.password_stdin:
            password = getpass.getpass(f"Password for camera user {a.camera_user}: ")
        if not password:
            missing.append("camera password (--password-stdin or YOOSEE_CAMERA_PASSWORD)")
    if missing:
        fail("missing required values: " + ", ".join(missing)
             + ". Provide them as flags/env vars, or run from a terminal for prompts.")
    if password and any(c in password for c in "\r\n"):
        fail("password must not contain newlines")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", a.camera_name):
        fail("--camera-name may only contain letters, digits, '_' and '-'")
    for net in a.allowed_network:
        try:
            ipaddress.ip_network(net, strict=False)
        except ValueError:
            fail(f"invalid --allowed-network {net!r}")
    for s in a.san:
        if not re.fullmatch(r"(DNS|IP):[^\s,]+", s):
            fail(f"invalid --san {s!r}; use DNS:name or IP:address")
    return password


def dependencies():
    problems = []
    if sys.version_info < (3, 11):
        problems.append("Python 3.11+ is required (this is %d.%d)" % sys.version_info[:2])
    for tool in ("ffmpeg", "ffprobe", "openssl"):
        if not shutil.which(tool):
            problems.append(f"{tool} not found")
    return problems


def install_hint():
    ident, like = "", ""
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            k, _, v = line.partition("=")
            v = v.strip().strip('"')
            if k == "ID":
                ident = v
            elif k == "ID_LIKE":
                like = v
    except OSError:
        pass
    names = set((ident + " " + like).split())
    if names & {"debian", "ubuntu"}:
        return "sudo apt install python3 ffmpeg openssl"
    if "arch" in names:
        return "sudo pacman -S python ffmpeg openssl"
    return "install Python 3.11+, ffmpeg (with ffprobe) and openssl"


def asset_constants():
    text = (SCRIPTS / "cache-web-assets.py").read_text()
    version = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.M)
    digest = re.search(r'^JS_SHA256\s*=\s*"([0-9a-f]{64})"', text, re.M)
    if not version or not digest:
        fail("could not read VERSION/JS_SHA256 from scripts/cache-web-assets.py")
    return version.group(1), digest.group(1)


def q(value):
    return json.dumps(str(value))


def render_toml(a, creds_file, hls_js, hls_sha, tls_cert, tls_key, prefix):
    nets = ", ".join(q(n) for n in a.allowed_network)
    cache = Path("~/.cache/yoosee-cam-utils").expanduser()
    return f"""credentials_file = {q(creds_file)}

[cameras.{a.camera_name}]
host = {q(a.camera_host)}
onvif_port = {a.onvif_port}
rtsp_port = {a.rtsp_port}
username_env = "{prefix}_USERNAME"
password_env = "{prefix}_PASSWORD"

[web]
bind = {q(a.bind)}
port = {a.port}
allowed_networks = [{nets}]
allowed_origins = []
auth_database = {q(a.state_dir / "viewer-users.sqlite3")}
tls_cert = {q(tls_cert)}
tls_key = {q(tls_key)}
cache_dir = {q(cache / "hls-runtime")}
hls_js = {q(hls_js)}
hls_js_sha256 = "{hls_sha}"

[recording]
enabled = false
spool_dir = {q(a.state_dir / "recordings")}
max_bytes = 2147483648
reserve_bytes = 5368709120
segment_seconds = 900
default_session_seconds = 3600
max_concurrent_sessions = 4
"""


def write_new(path, text, mode, a, label):
    """Create a file atomically with the given mode; skip if it exists unless --force."""
    if a.dry_run:
        say(f"[dry-run] would write {label}: {path} (mode {mode:04o})")
        return "planned"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not a.force:
            say(f"skipped {label}: {path} exists (use --force to replace)")
            return "skipped"
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    say(f"wrote {label}: {path}")
    return "written"


def make_private_dir(path, a):
    if a.dry_run:
        say(f"[dry-run] would create directory {path} (mode 0700)")
        return
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def run(cmd, label, a, fatal=True):
    say(f"$ {' '.join(str(c) for c in cmd)}")
    if a.dry_run and fatal is None:
        return True
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        msg = f"{label} failed (exit {r.returncode})"
        if fatal:
            fail(msg)
        say(f"warning: {msg}")
        return False
    return True


def make_tls(a, cert, key):
    if a.skip_tls:
        say("skipped TLS: --skip-tls")
        return
    if a.dry_run:
        say(f"[dry-run] would create TLS key {key} (EC P-256, mode 0600) and cert {cert} (825 days, SAN {','.join(a.san)})")
        return
    exists = [p for p in (cert, key) if p.exists()]
    if exists and not a.force:
        say(f"skipped TLS: {', '.join(map(str, exists))} exists (use --force to replace)")
        return
    cert.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for p in exists:
        p.unlink()
    old = os.umask(0o077)
    try:
        r = subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
             "-nodes", "-days", "825", "-subj", "/CN=yoosee-cam-utils",
             "-addext", "subjectAltName=" + ",".join(a.san),
             "-keyout", str(key), "-out", str(cert)],
            capture_output=True, text=True)
    finally:
        os.umask(old)
    if r.returncode != 0:
        fail("openssl failed: " + r.stderr.strip())
    os.chmod(key, 0o600)
    os.chmod(cert, 0o644)
    say(f"wrote TLS key {key} and certificate {cert}")


def main(argv=None):
    a = parse_args(argv)
    password = resolve_inputs(a)
    prefix = env_prefix(a.camera_name)
    if a.dry_run:
        say("Dry run: nothing will be written or changed.")

    say("Step 1: dependencies")
    problems = dependencies()
    if problems:
        for p in problems:
            say(f"  missing: {p}")
        say(f"  fix: {install_hint()}")
        if not a.dry_run:
            fail("dependency check failed")
    else:
        say("  requirements available")

    tls_dir = a.config_dir / "tls"
    cert, key = tls_dir / "server.crt", tls_dir / "server.key"
    version, sha = asset_constants()
    hls_js = Path("~/.cache/yoosee-cam-utils").expanduser() / f"hls-v{version}" / "hls.min.js"

    say("Step 2: credentials file")
    cred_text = f"{prefix}_USERNAME={a.camera_user}\n{prefix}_PASSWORD={password or ''}\n"
    write_new(a.credentials_file, cred_text, 0o600, a, "credentials file")

    say("Step 3: configuration")
    make_private_dir(a.config_dir, a)
    make_private_dir(a.repo_config.parent, a)
    toml_text = render_toml(a, a.credentials_file, hls_js, sha, cert, key, prefix)
    write_new(a.repo_config, toml_text, 0o600, a, "config")
    if not a.dry_run:
        a.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    say("Step 4: TLS certificate")
    make_tls(a, cert, key)

    say("Step 5: web assets")
    if a.skip_assets:
        say("skipped: --skip-assets")
    elif a.dry_run:
        say("[dry-run] would run: python3 scripts/cache-web-assets.py --apply")
    else:
        run([sys.executable, SCRIPTS / "cache-web-assets.py", "--apply"], "asset caching", a)

    say("Step 6: camera check")
    if a.skip_check:
        say("skipped: --skip-check")
    elif a.dry_run:
        say("[dry-run] would run: scripts/yoosee-cam doctor and profiles")
    else:
        base = [SCRIPTS / "yoosee-cam", "--config", a.repo_config, "--camera", a.camera_name]
        ok = run(base + ["doctor"], "camera doctor", a, fatal=False)
        ok = run(base + ["profiles"], "camera profiles", a, fatal=False) and ok
        if not ok:
            say("warning: camera check failed; fix host, ports or credentials and rerun.")

    say("Step 7: web dry run")
    if a.dry_run:
        say(f"[dry-run] would run: scripts/yoosee-web --config {a.repo_config} --dry-run")
    else:
        run([SCRIPTS / "yoosee-web", "--config", a.repo_config, "--dry-run"], "web dry run", a, fatal=False)

    if a.install_service:
        say("Step 8: user service")
        cmd = [sys.executable, SCRIPTS / "install-web-service.py", "--config", a.repo_config]
        if not a.dry_run:
            cmd.append("--apply")
        if a.dry_run:
            say(f"[dry-run] would run: {' '.join(map(str, cmd))} (--apply added when not a dry run)")
        else:
            run(cmd, "service install", a, fatal=False)
        if not env("DISPLAY") and not env("WAYLAND_DISPLAY"):
            say('No graphical session detected. Keep the service running after logout with:\n  sudo loginctl enable-linger "$USER"')

    say("\nSummary")
    say(f"  config:      {a.repo_config}")
    say(f"  credentials: {a.credentials_file}")
    say(f"  TLS:         {cert}")
    say("Next commands:")
    say(f"  scripts/yoosee-cam --config {a.repo_config} doctor")
    say(f"  scripts/yoosee-web --config {a.repo_config}")
    if not a.install_service:
        say(f"  scripts/install-web-service.py --config {a.repo_config} --apply")
    say(f"Open https://{a.bind}:{a.port}/ . The first web sign-in uses the camera's own username and password.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
