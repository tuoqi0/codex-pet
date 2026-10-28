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
Version: 2.8.0
Section: utils
Priority: optional
Architecture: all
Maintainer: Codex Pet <noreply@example.invalid>
Depends: python3, python3-pyqt5, python3-pil, python3-requests, python3-jwt, python3-cryptography
Description: Interactive dragon desktop companion
 A dragon companion with greeting poses, blinking, desktop patrol,
 and a draggable transparent window.
EOF
for source in "$base"/src/*.py; do
    install -m 644 "$source" "$stage/usr/share/codex-pet/"
done
for asset in dragon-lady.png dragon-seated.png dragon-seated-blink.png dragon-seated-wave.png dragon-seated-wave2.png dragon-seated-tea.png dragon-seated-happy.png dragon-seated-work.png; do
    install -m 644 "$base/assets/$asset" "$stage/usr/share/codex-pet/$asset"
done
install -m 644 "$base/assets/dragon-lady.png" "$stage/usr/share/pixmaps/codex-pet.png"
cat > "$stage/usr/bin/codex-pet" <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /usr/share/codex-pet/codex_pet.py "$@"
EOF
chmod 755 "$stage/usr/bin/codex-pet"
cat > "$stage/usr/bin/codex-pet-import" <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /usr/share/codex-pet/import_bailong.py "$@"
EOF
chmod 755 "$stage/usr/bin/codex-pet-import"
install -m 644 "$base/packaging/codex-pet.desktop" "$stage/usr/share/applications/"
dpkg-deb --build --root-owner-group "$stage" "$out/codex-pet_2.8.0_all.deb"
