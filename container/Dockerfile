# momo-box image: momo on top of any glibc base image.
#
#   docker build -f container/Dockerfile -t momo-harness .                 (Debian + git)
#   docker build -f container/Dockerfile --build-arg BASE=gradle:8-jdk21 \
#                --build-arg PACKAGES="cmake" -t my-box .                   (a toolchain)
#
# The launcher (container/momo_box.py) picks BASE and PACKAGES from the project
# (.momo/box.json, devcontainer.json or detection) and builds this for you.
#
# momo lives entirely under /opt/momo with its own Python, which is NOT on PATH: the
# model's run_command sees the base image's tools only.  That Python is the
# official image's (Docker Official Images, like the Debian base), pinned by
# digest, made self-contained by container/bundle-python.sh: nothing is
# downloaded for it.  `container/wheels/update.py --python-image` moves the pin.
ARG BASE=docker.io/library/debian:bookworm-slim
ARG PY_VERSION=3.14.10
ARG PY_IMAGE=docker.io/library/python:3.14.10-slim-bookworm@sha256:3fe1a0b6c2d6b2ebb572906cdabeca75aeb558c0c17db5ffefbcf8094871c32b

# ── stage 1: momo + a self-contained CPython ─────────────────────────────────
FROM ${PY_IMAGE} AS momo-runtime
ARG TARGETARCH
ARG WITH_EXTRAS=1
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends patchelf \
 && rm -rf /var/lib/apt/lists/*
COPY container/bundle-python.sh /tmp/bundle-python.sh
RUN sh /tmp/bundle-python.sh /usr/local /opt/momo/python
# The optional extras (tree-sitter grammars, pypdf) go into momo's own Python, from
# the wheels committed in container/wheels/<arch>: no index is asked, and every file
# must match its hash in requirements.lock (container/wheels/update.py refreshes
# them).  A grammar with no wheel for this platform is left out there, and momo
# runs without it.
COPY container/wheels /tmp/wheels
RUN set -eu; \
    if [ "$WITH_EXTRAS" = "1" ]; then \
      arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
      [ -f "/tmp/wheels/$arch/requirements.lock" ] \
        || { echo "momo-box: no vendored wheels for $arch (container/wheels/update.py)" >&2; exit 1; }; \
      /opt/momo/python/bin/python3 -m pip install -q --no-cache-dir --disable-pip-version-check \
        --no-index --find-links "/tmp/wheels/$arch" --only-binary :all: --require-hashes \
        -r "/tmp/wheels/$arch/requirements.lock"; \
    fi; \
    rm -rf /tmp/wheels
COPY momo-coding-harness.py /opt/momo/app/
COPY harness /opt/momo/app/harness
COPY roles /opt/momo/app/roles
COPY skills /opt/momo/app/skills
COPY search_sources /opt/momo/app/search_sources
COPY container /opt/momo/app/container
# The root filesystem is read-only at run time: compile now.
# (the wheels were installed above: not shipped twice)
RUN rm -rf /opt/momo/app/container/wheels \
 && /opt/momo/python/bin/python3 -m compileall -q /opt/momo/app /opt/momo/python/lib >/dev/null || true

# ── stage 2: the toolchain base + momo ───────────────────────────────────────
FROM ${BASE}
ARG PACKAGES=""
USER root
RUN if ls /lib/ld-musl-* >/dev/null 2>&1; then \
      echo "momo-box: this base image uses musl (Alpine); use a glibc base (Debian, Ubuntu, Fedora…)" >&2; \
      exit 1; \
    fi
COPY container/pkg-install.sh /usr/local/bin/momo-pkg-install
RUN sh /usr/local/bin/momo-pkg-install git ca-certificates $PACKAGES
# uid 1000 runs momo: a new user `momo`, or the image's own uid-1000 user (node,
# ubuntu, gradle…) with its home moved to /home/momo. The passwd entry matters, not
# just $HOME: Java takes user.home from it (Maven's ~/.m2, Gradle's ~/.gradle), and
# the rest of the root filesystem is read-only at run time.
RUN set -eu; \
    if u="$(getent passwd 1000 | cut -d: -f1)" && [ -n "$u" ]; then \
      usermod -d /home/momo "$u" 2>/dev/null \
        || sed -i "s#^\($u:[^:]*:1000:[^:]*:[^:]*:\)[^:]*#\1/home/momo#" /etc/passwd; \
    else \
      useradd -m -u 1000 -s /bin/sh momo 2>/dev/null || adduser -D -u 1000 momo; \
    fi; \
    mkdir -p /home/momo /workspace /gw /in; \
    chown 1000:1000 /home/momo /workspace /gw
COPY --from=momo-runtime /opt/momo /opt/momo
# Debian's OpenSSL (bundled with momo's Python) looks for CA certificates under
# /usr/lib/ssl, which only Debian and Ubuntu have; elsewhere point it at /etc/ssl.
# Then a smoke test: the bundled Python needs at least the glibc in
# /opt/momo/python/GLIBC_FLOOR (bundle-python.sh measured it).
RUN [ -e /usr/lib/ssl ] || ln -s /etc/ssl /usr/lib/ssl; \
    /opt/momo/python/bin/python3 -S -c 'import ssl, sqlite3, ctypes, lzma, bz2, zlib, json, hashlib; ssl.create_default_context()' \
      || { echo "momo-box: momo's Python needs glibc $(cat /opt/momo/python/GLIBC_FLOOR) or newer and does not run on this base image ($(ldd --version 2>&1 | head -1)); pick a newer base" >&2; exit 1; }
ENV HOME=/home/momo
USER 1000:1000
WORKDIR /workspace
VOLUME ["/workspace", "/home/momo"]
STOPSIGNAL SIGTERM
# -S: the entrypoint is stdlib-only, and it stays in memory as momo's supervisor.
ENTRYPOINT ["/opt/momo/python/bin/python3", "-S", "/opt/momo/app/container/entrypoint.py"]
