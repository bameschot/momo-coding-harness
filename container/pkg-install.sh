#!/bin/sh
# Install system packages with whatever package manager the base image has.
# Used by container/Dockerfile: momo-pkg-install git ca-certificates cmake …
set -eu
[ "$#" -gt 0 ] || exit 0
if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends "$@"
    rm -rf /var/lib/apt/lists/*
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q "$@"
    dnf clean all
elif command -v microdnf >/dev/null 2>&1; then
    microdnf install -y "$@"
    microdnf clean all
elif command -v yum >/dev/null 2>&1; then
    yum install -y -q "$@"
    yum clean all
elif command -v zypper >/dev/null 2>&1; then
    zypper --non-interactive install --no-recommends "$@"
else
    echo "momo-box: the base image has no known package manager; install $* in a" \
         ".momo/box.json \"dockerfile\" instead" >&2
    exit 1
fi
