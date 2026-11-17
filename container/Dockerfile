# momo-box image: momo on top of any glibc base image.
#
#   docker build -f container/Dockerfile -t momo-harness .                 (Debian + git)
#   docker build -f container/Dockerfile --build-arg BASE=gradle:8-jdk21 \
#                --build-arg PACKAGES="cmake" -t my-box .                   (a toolchain)
#
# The launcher (container/momo_box.py) picks BASE and PACKAGES from the project
# (.momo/box.json, devcontainer.json or detection) and builds this for you.
#
# momo lives entirely under /opt/momo with its own Python (python-build-standalone),
# which is NOT on PATH: the model's run_command sees the base image's tools only.
ARG BASE=debian:bookworm-slim

# ── stage 1: momo + a relocatable CPython ────────────────────────────────────
FROM debian:bookworm-slim AS momo-runtime
ARG TARGETARCH
ARG PBS_TAG=20261026
ARG PY_VERSION=3.14.10
ARG PBS_SHA256_AMD64=4d3c16c59ed0802a3d1d34d8bce504b15ceeaff39d62dadecc6a9fa803412aed
ARG PBS_SHA256_ARM64=90f48337bc3dd4ae6eda81e001e4c981236d403b6b5a07778bbdf94ee423dc00
ARG WITH_EXTRAS=1
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
RUN set -eu; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
      amd64) triple=x86_64-unknown-linux-gnu;  sum="$PBS_SHA256_AMD64" ;; \
      arm64) triple=aarch64-unknown-linux-gnu; sum="$PBS_SHA256_ARM64" ;; \
      *) echo "momo-box: unsupported architecture $arch" >&2; exit 1 ;; \
    esac; \
    url="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/cpython-${PY_VERSION}+${PBS_TAG}-${triple}-install_only_stripped.tar.gz"; \
    curl -fsSL "$url" -o /tmp/python.tar.gz; \
    echo "$sum  /tmp/python.tar.gz" | sha256sum -c -; \
    mkdir -p /opt/momo; \
    tar -xzf /tmp/python.tar.gz -C /opt/momo; \
    rm /tmp/python.tar.gz
COPY requirements.txt /opt/momo/app/requirements.txt
# The optional extras (tree-sitter grammars, pypdf) go into momo's own Python.
# Wheels only (no compiler here).  A grammar without a wheel for this platform
# (e.g. tree-sitter-dockerfile on linux/arm64) is skipped and momo runs without it;
# tree-sitter itself failing (no network, a broken index) fails the build.
RUN if [ "$WITH_EXTRAS" = "1" ]; then \
      pip="/opt/momo/python/bin/python3 -m pip install -q --no-cache-dir --disable-pip-version-check --only-binary :all:"; \
      $pip -r /opt/momo/app/requirements.txt 2>/dev/null || { \
        echo "momo-box: installing the extras one at a time" >&2; \
        sed -e 's/#.*//' -e '/^[[:space:]]*$/d' /opt/momo/app/requirements.txt | while read -r req; do \
          $pip "$req" || case "$req" in \
            tree-sitter[!-]*|tree-sitter) echo "momo-box: tree-sitter could not be installed (see the error above)" >&2; exit 1 ;; \
            *) echo "momo-box: optional $req skipped (no wheel for this platform)" >&2 ;; \
          esac; \
        done || exit 1; \
      }; \
    fi
COPY momo-coding-harness.py /opt/momo/app/
COPY harness /opt/momo/app/harness
COPY roles /opt/momo/app/roles
COPY skills /opt/momo/app/skills
COPY search_sources /opt/momo/app/search_sources
COPY container /opt/momo/app/container
# The root filesystem is read-only at run time: compile now.
RUN /opt/momo/python/bin/python3 -m compileall -q /opt/momo/app /opt/momo/python/lib >/dev/null || true

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
ENV HOME=/home/momo
USER 1000:1000
WORKDIR /workspace
VOLUME ["/workspace", "/home/momo"]
STOPSIGNAL SIGTERM
# -S: the entrypoint is stdlib-only, and it stays in memory as momo's supervisor.
ENTRYPOINT ["/opt/momo/python/bin/python3", "-S", "/opt/momo/app/container/entrypoint.py"]
