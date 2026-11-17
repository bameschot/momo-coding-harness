#!/bin/sh
# Make the official Python image's interpreter (/usr/local, Debian) self-contained
# under DEST, for any glibc base image (momo-box image, stage 1):
#
#   bundle-python.sh /usr/local /opt/momo/python
#
# The interpreter finds libpython through its own $ORIGIN/../lib rpath, but its
# extension modules (ssl, sqlite3, ctypes, lzma, readline…) link Debian's shared
# libraries.  Every library they need, except glibc's own, is copied into
# DEST/lib and found there through an rpath ($ORIGIN-relative, so nothing is set
# in the environment that the model's commands would inherit).  The result is
# checked: no module may resolve a library from outside DEST but glibc.
set -eu
src=$1
dst=$2
# glibc and the compiler runtime come from the base image (that is the floor).
system='^(linux-vdso\.so|ld-linux|libc\.so|libm\.so|libpthread\.so|libdl\.so|librt\.so|libutil\.so|libresolv\.so|libanl\.so|libnsl\.so|libgcc_s\.so)'

mkdir -p "$dst"
cp -a "$src/bin" "$src/lib" "$dst/"
dynload=$(echo "$dst"/lib/python3.*/lib-dynload)
rm -rf "$dst"/lib/pkgconfig "$dst"/lib/python3.*/config-*
# tkinter: the slim image has no Tcl/Tk, so the module can't load anyway.
rm -f "$dynload"/_tkinter*.so

objects() {
    find "$dst/lib" -maxdepth 1 -name 'libpython*.so*' -type f
    find "$dynload" -name '*.so' -type f
}

# The libraries (ldd lists the whole closure), each copied once.
for obj in $(objects); do
    ldd "$obj" | awk '$2 == "=>" && $3 ~ /^\// {print $1, $3}'
done | sort -u | while read -r name path; do
    if ! echo "$name" | grep -Eq "$system" && [ ! -e "$dst/lib/$name" ]; then
        cp -L "$path" "$dst/lib/$name"
        chmod 0644 "$dst/lib/$name"
        patchelf --set-rpath '$ORIGIN' "$dst/lib/$name"
    fi
done
for so in "$dynload"/*.so; do
    patchelf --set-rpath '$ORIGIN/../..' "$so"
done
# libpython3.so (the stable-ABI stub) links libpython3.X.so by name.
for lib in "$dst"/lib/libpython*.so*; do
    [ -L "$lib" ] || patchelf --set-rpath '$ORIGIN' "$lib"
done

# The check: every non-glibc library resolves inside DEST, none is missing.
bad=0
for obj in $(objects) $(find "$dst/lib" -maxdepth 1 -name '*.so*' -type f); do
    ldd "$obj" | awk -v dst="$dst/" -v sysre="$system" -v obj="$obj" '
        $0 ~ /not found/ { print "momo-box: " obj ": " $1 " not found"; bad = 1 }
        $2 == "=>" && $3 ~ /^\// && index($3, dst) != 1 && $1 !~ sysre {
            print "momo-box: " obj " uses " $3 " from the build image"; bad = 1 }
        END { exit bad }' || bad=1
done
[ "$bad" = 0 ] || exit 1

# The glibc this needs (the newest GLIBC_2.x symbol version any file uses): the
# second stage's smoke test names it when a base image is too old.
"$dst/bin/python3" -S - "$dst" > "$dst/GLIBC_FLOOR" <<'EOF'
import os, re, sys
best = (2, 0)
for root, _, files in os.walk(sys.argv[1]):
    for name in files:
        path = os.path.join(root, name)
        if os.path.islink(path) or not (".so" in name or name.startswith("python3")):
            continue
        with open(path, "rb") as f:
            for minor, patch in re.findall(rb"GLIBC_2\.(\d+)(?:\.(\d+))?", f.read()):
                best = max(best, (int(minor), int(patch or 0)))
print("2.%d" % best[0] + (".%d" % best[1] if best[1] else ""))
EOF
echo "momo-box: Python bundled in $dst ($(du -sh "$dst" | cut -f1), needs glibc $(cat "$dst/GLIBC_FLOOR"))"
