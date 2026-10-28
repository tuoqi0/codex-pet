#!/bin/sh
set -eu
umask 022
base=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
out=${1:-"$base/dist"}
mkdir -p "$out"
out=$(CDPATH= cd -- "$out" && pwd)
stage=$(mktemp -d)
chmod 755 "$stage"
trap 'rm -rf "$stage"' EXIT HUP INT TERM
mkdir -p "$stage/DEBIAN" "$stage/usr/bin" "$stage/usr/share/codex-pet" \
    "$stage/usr/share/applications" "$stage/usr/share/pixmaps"
cat > "$stage/DEBIAN/control" <<'EOF'
Package: codex-pet
Version: 1.2.0
Section: utils
Priority: optional
Architecture: all
Maintainer: Codex Pet <noreply@example.invalid>
Depends: python3, python3-pyqt5
Description: Interactive dragon desktop companion
 A dragon companion with greeting poses, blinking, desktop patrol,
 and a draggable transparent window.
EOF
install -m 755 "$base/src/codex_pet.py" "$stage/usr/share/codex-pet/codex_pet.py"
install -m 644 "$base/src/pet_behavior.py" "$stage/usr/share/codex-pet/pet_behavior.py"
for asset in dragon-lady.png blink.png wave.png wave2.png; do
    install -m 644 "$base/assets/$asset" "$stage/usr/share/codex-pet/$asset"
done
install -m 644 "$base/assets/dragon-lady.png" "$stage/usr/share/pixmaps/codex-pet.png"
cat > "$stage/usr/bin/codex-pet" <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /usr/share/codex-pet/codex_pet.py "$@"
EOF
chmod 755 "$stage/usr/bin/codex-pet"
install -m 644 "$base/packaging/codex-pet.desktop" "$stage/usr/share/applications/"
dpkg-deb --build --root-owner-group "$stage" "$out/codex-pet_1.2.0_all.deb"
