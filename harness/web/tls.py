"""HTTPS for the web UI: momo's own local certificate authority (``--web-tls auto``).

Python's ssl module can load certificates but not create them, so this shells out
to the ``openssl`` command (OpenSSL or LibreSSL — only options both understand are
used).  It works like a small mkcert:

* once, a root CA (``momo-ca.pem`` / ``momo-ca.key``) that the user trusts once per
  device;
* a server certificate signed by it for this machine's names and LAN addresses,
  re-issued whenever those change or it nears expiry, without a new trust step.

The CA carries X.509 name constraints: it may only vouch for ``localhost``,
``*.local``, this host's name, private and loopback addresses, and the extra
names it was created with.  A device that trusts it therefore cannot be fooled
into accepting a certificate for a public site, even if the CA key leaks.

Everything lives in ``~/.momo-harness/tls/`` (directory 0700, keys 0600).  Files
not owned by the user, or writable by others, are refused.  No openssl output
reaches the terminal: stderr would corrupt the curses TUI.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import secrets
import shutil
import socket
import ssl
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

CA_DAYS = 3650
SERVER_DAYS = 397            # browsers cap leaf lifetimes; 397 is accepted everywhere
RENEW_BEFORE_S = 30 * 86400

# Address ranges the CA may vouch for: loopback, private, CGNAT (Tailscale), IPv6 ULA.
_PERMITTED_NETS = ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                   "100.64.0.0/10", "::1/128", "fc00::/7")
_BASE_DNS = ("localhost", "local")   # "local" permits every *.local name


class TLSError(OSError):
    """A certificate could not be created or used; the message says why."""


@dataclass
class AutoCert:
    cert: Path
    key: Path
    ca: Path
    ca_fingerprint: str
    names: list[str]
    issued: bool                # a new server certificate was made this time


def tls_dir() -> Path:
    return Path.home() / ".momo-harness" / "tls"


def fingerprint(pem_path: Path) -> str:
    """SHA-256 of the certificate, as colon-separated hex (what browsers show)."""
    der = ssl.PEM_cert_to_DER_cert(pem_path.read_text())
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


# ── names ────────────────────────────────────────────────────────────────────

def _ip(name: str):
    try:
        return ipaddress.ip_address(name.strip("[]"))
    except ValueError:
        return None


def _norm(name: str) -> str:
    ip = _ip(name)
    return str(ip) if ip else name.strip().rstrip(".").lower()


def _primary_ip() -> str | None:
    """The address this machine uses for its default route. Connecting a UDP socket
    sends nothing; it only makes the kernel pick a source address."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))          # TEST-NET-1, never actually contacted
            return s.getsockname()[0]
    except OSError:
        return None


def local_names(bind_host: str = "", extra=()) -> list[str]:
    """Names and addresses this machine answers to, most useful first:
    localhost, the hostname and its .local form, the bind address, LAN addresses,
    then any extra names given by the user."""
    out: list[str] = []

    def add(n):
        if n and (n := _norm(n)) and n not in out:
            out.append(n)

    for n in ("localhost", "127.0.0.1", "::1"):
        add(n)
    host = socket.gethostname()
    add(host)
    short = host.split(".")[0]
    if short and not _ip(host):
        add(f"{short}.local")
    ip = _ip(bind_host)
    if ip is not None and not ip.is_unspecified:
        add(str(ip))
    add(_primary_ip())
    try:
        for info in socket.getaddrinfo(host, None):
            addr = info[4][0].split("%")[0]      # drop an IPv6 zone id
            if not (_ip(addr) and _ip(addr).is_link_local):
                add(addr)
    except OSError:
        pass
    for n in extra:
        add(n)
    return out


# ── constraints ──────────────────────────────────────────────────────────────

def _permits(permitted: dict, name: str) -> bool:
    ip = _ip(name)
    if ip is not None:
        return any(ip in ipaddress.ip_network(net) for net in permitted["nets"]
                   if ipaddress.ip_network(net).version == ip.version)
    return any(name == d or name.endswith("." + d) for d in permitted["dns"])


def _constraint_lines(permitted: dict) -> list[str]:
    lines = [f"permitted;DNS.{i} = {d}" for i, d in enumerate(permitted["dns"])]
    for i, net in enumerate(permitted["nets"]):
        n = ipaddress.ip_network(net)
        lines.append(f"permitted;IP.{i} = {n.network_address}/{n.netmask}")
    return lines


# ── files ────────────────────────────────────────────────────────────────────

def _check_owned(path: Path) -> None:
    st = path.stat()
    if st.st_uid != os.getuid() or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise TLSError(f"refusing {path}: it must be owned by you and not writable by others")


def _prepare_dir(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    _check_owned(d)


def _openssl(openssl: str, *args: str, cwd: Path) -> None:
    try:
        r = subprocess.run([openssl, *args], cwd=cwd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise TLSError(f"could not run {openssl}: {e}") from None
    if r.returncode != 0:
        detail = (r.stderr or r.stdout).strip().splitlines()
        raise TLSError(f"openssl {args[0]} failed: {detail[-1] if detail else r.returncode}")


def _new_key(openssl: str, out: Path) -> None:
    _openssl(openssl, "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", out.name,
             cwd=out.parent)
    os.chmod(out, 0o600)


def _serial() -> str:
    return "0x" + secrets.token_hex(15)


def _find_openssl(openssl: str | None) -> str:
    found = openssl or shutil.which("openssl")
    if not found:
        raise TLSError("--web-tls auto needs the openssl command; install it, "
                       "or pass your own certificate with --web-cert/--web-key")
    return found


# ── CA ───────────────────────────────────────────────────────────────────────

def _ensure_ca(openssl: str, d: Path, extra: list[str]) -> dict:
    ca_pem, ca_key, ca_json = d / "momo-ca.pem", d / "momo-ca.key", d / "momo-ca.json"
    if ca_pem.exists() and ca_key.exists() and ca_json.exists():
        for p in (ca_pem, ca_key, ca_json):
            _check_owned(p)
        return json.loads(ca_json.read_text())

    host = _norm(socket.gethostname())
    dns = list(_BASE_DNS)
    nets = list(_PERMITTED_NETS)
    for n in [host, *extra]:
        ip = _ip(n)
        if ip is not None:
            net = str(ipaddress.ip_network(ip))
            if not any(ip in ipaddress.ip_network(x) for x in nets
                       if ipaddress.ip_network(x).version == ip.version):
                nets.append(net)
        elif not any(n == x or n.endswith("." + x) for x in dns):
            dns.append(n)
    permitted = {"dns": dns, "nets": nets}

    conf = "\n".join([
        "[req]", "distinguished_name = dn", "[dn]",
        "[v3_ca]",
        "basicConstraints = critical,CA:true,pathlen:0",
        "keyUsage = critical,keyCertSign,cRLSign",
        "subjectKeyIdentifier = hash",
        "authorityKeyIdentifier = keyid:always",
        "nameConstraints = critical,@nc",
        "[nc]", *_constraint_lines(permitted), "",
    ])
    with tempfile.TemporaryDirectory(dir=d) as tmp:
        t = Path(tmp)
        (t / "ca.cnf").write_text(conf)
        _new_key(openssl, t / "ca.key")
        cn = f"momo local CA ({socket.gethostname().split('.')[0]})"[:64]
        _openssl(openssl, "req", "-x509", "-new", "-key", "ca.key", "-sha256",
                 "-days", str(CA_DAYS), "-set_serial", _serial(), "-subj", f"/CN={cn}",
                 "-config", "ca.cnf", "-extensions", "v3_ca", "-out", "ca.pem", cwd=t)
        (t / "ca.json").write_text(json.dumps(permitted, indent=1))
        os.replace(t / "ca.key", ca_key)
        os.replace(t / "ca.pem", ca_pem)
        os.replace(t / "ca.json", ca_json)
    for f in (d / "server.pem", d / "server.key", d / "server.json"):
        f.unlink(missing_ok=True)       # signed by a CA that no longer exists
    return permitted


# ── server certificate ───────────────────────────────────────────────────────

def _issue_server(openssl: str, d: Path, names: list[str]) -> None:
    alt = []
    for i, n in enumerate(names):
        alt.append(f"IP.{i} = {n}" if _ip(n) else f"DNS.{i} = {n}")
    conf = "\n".join([
        "[req]", "distinguished_name = dn", "[dn]",
        "[v3_srv]",
        "basicConstraints = critical,CA:false",
        "keyUsage = critical,digitalSignature",
        "extendedKeyUsage = serverAuth",
        "subjectKeyIdentifier = hash",
        "authorityKeyIdentifier = keyid:always",
        "subjectAltName = @alt",
        "[alt]", *alt, "",
    ])
    with tempfile.TemporaryDirectory(dir=d) as tmp:
        t = Path(tmp)
        (t / "srv.cnf").write_text(conf)
        _new_key(openssl, t / "server.key")
        _openssl(openssl, "req", "-new", "-key", "server.key", "-subj", "/CN=momo",
                 "-config", "srv.cnf", "-out", "server.csr", cwd=t)
        _openssl(openssl, "x509", "-req", "-in", "server.csr",
                 "-CA", str(d / "momo-ca.pem"), "-CAkey", str(d / "momo-ca.key"),
                 "-set_serial", _serial(), "-days", str(SERVER_DAYS), "-sha256",
                 "-extfile", "srv.cnf", "-extensions", "v3_srv", "-out", "server.pem", cwd=t)
        meta = {"names": names, "ca": fingerprint(d / "momo-ca.pem"),
                "expires": time.time() + SERVER_DAYS * 86400}
        (t / "server.json").write_text(json.dumps(meta, indent=1))
        os.replace(t / "server.key", d / "server.key")
        os.replace(t / "server.pem", d / "server.pem")
        os.replace(t / "server.json", d / "server.json")


def ensure(bind_host: str = "", extra=(), *, openssl: str | None = None,
           directory: Path | None = None) -> AutoCert:
    """Create or reuse momo's CA and a server certificate for this machine.
    Raises TLSError with a readable message on any problem."""
    openssl = _find_openssl(openssl)
    d = directory or tls_dir()
    _prepare_dir(d)
    extra = [_norm(n) for n in extra]
    permitted = _ensure_ca(openssl, d, extra)

    outside = [n for n in extra if not _permits(permitted, n)]
    if outside:
        raise TLSError(
            f"momo's CA was created without {', '.join(outside)}, and its name constraints "
            f"forbid adding names later. Delete {d / 'momo-ca.pem'} and momo-ca.key to "
            f"make a new CA (every device must trust it again), or use --web-cert/--web-key.")
    # Auto-detected names the CA may not vouch for (e.g. a public address) are left out.
    names = [n for n in local_names(bind_host, extra) if _permits(permitted, n)]

    ca_fp = fingerprint(d / "momo-ca.pem")
    srv, key, meta_path = d / "server.pem", d / "server.key", d / "server.json"
    issued = False
    meta = None
    if srv.exists() and key.exists() and meta_path.exists():
        for p in (srv, key, meta_path):
            _check_owned(p)
        try:
            meta = json.loads(meta_path.read_text())
        except ValueError:
            meta = None
    if (meta is None or meta.get("names") != names or meta.get("ca") != ca_fp
            or meta.get("expires", 0) - time.time() < RENEW_BEFORE_S):
        _issue_server(openssl, d, names)
        issued = True
    return AutoCert(cert=srv, key=key, ca=d / "momo-ca.pem", ca_fingerprint=ca_fp,
                    names=names, issued=issued)
