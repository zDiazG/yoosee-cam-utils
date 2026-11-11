# Security model

Watchpost is meant for a private network or a private overlay network, run by one
operator on a host they control. This page lists what its controls protect, and what
they leave to the host and network.

## Network clients

These controls apply to anyone who can reach the HTTPS port:

- TLS 1.2 or newer on every connection. There is no plain HTTP listener.
- A global source allowlist (`web.allowed_networks`). Sign-in, session use and every
  API or media request require an allowed source address. The login page and static
  scripts are served to any client that completes TLS. A default route (`0.0.0.0/0`)
  is rejected.
- Optional per-account source networks inside the global list.
- Passwords hashed with scrypt and a per-account salt. At most two hashes run at the
  same time, and 12 failed sign-ins per minute block that source address.
- Session cookies that are `Secure`, `HttpOnly` and `SameSite=Strict`, bound to the
  client address, with idle and absolute timeouts for normal accounts.
- CSRF protection on every state change: a custom header, an exact HTTPS `Origin`
  check and a per-session token.
- A restrictive Content Security Policy with no inline or third-party scripts.
- Server-side checks of camera grants on every API call and every 2 seconds on open
  live streams.
- `X-Forwarded-For` is accepted only from addresses in `web.trusted_proxies`.

The application allowlist is not a firewall. Bind to a specific address and limit
the port with the host firewall as well; a client that is blocked by the firewall
never reaches the TLS handshake.

## Cameras

ONVIF control traffic is plain HTTP and RTSP is unencrypted, because that is what
the cameras support. ONVIF uses a password digest, but anyone who can capture traffic
on the camera network can see or attack these sessions. Keep cameras on a network
segment that untrusted devices cannot reach. If the cameras do not need the vendor
cloud, block their internet access at the router.

The first web administrator starts with the camera's password. A captured camera
password therefore also opens the web administrator account until that web password
is changed. Change it in Settings if the camera network is shared.

## The host

Everything the service needs runs as one Unix account: the camera credentials file,
the TLS key, the account database and the clips. The service creates its files with
mode 0600 and directories with 0700, refuses group- or world-readable credentials
and keys, and runs with `UMask=0077`.

Any process running as that account, and root, can read all of it. On a workstation
used for other work, that includes the browser, editors and their extensions. The
service cannot defend against that. If the workstation runs untrusted software, run
Watchpost under a dedicated account with its own home directory, or on a separate
host.

The user service also sets `MemoryMax=512M` and restarts on failure. It does not
apply systemd sandboxing options yet.

## Not covered

- Denial of service from a reachable client. Limit who can reach the port.
- Multi-factor authentication and external identity providers.
- Audit logs of sign-ins and administrative changes.
- Encryption of clips at rest. Use disk encryption on the host.

## Reporting a problem

Do not post exploit details in a public issue. Open an issue that asks for a private
contact, and the details can be shared there.
