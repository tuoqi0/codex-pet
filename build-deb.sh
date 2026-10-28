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
Version: 2.25.0
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
for asset in dragon-lady.png opening-sprite.png dragon-seated.png dragon-seated-blink.png dragon-seated-wave.png dragon-seated-wave2.png dragon-seated-tea.png dragon-seated-happy.png dragon-seated-work.png wing-cutout.png wing-underlay.png wing-left.png wing-right.png; do
    install -m 644 "$base/assets/$asset" "$stage/usr/share/codex-pet/$asset"
done
install -m 644 "$base/assets/dragon-lady.png" "$stage/usr/share/pixmaps/codex-pet.png"
mkdir -p "$stage/usr/share/codex-pet/opening"
for source in "$base"/opening/patch.py "$base"/opening/opening.css "$base"/opening/opening.js "$base"/opening/desktop_entry.py "$base"/opening/desktop_splash.py; do
    install -m 644 "$source" "$stage/usr/share/codex-pet/opening/"
done
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
cat > "$stage/usr/bin/codex-pet-opening" <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /usr/share/codex-pet/opening/patch.py "$@"
EOF
chmod 755 "$stage/usr/bin/codex-pet-opening"
cat > "$stage/usr/bin/codex-pet-desktop-opening" <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /usr/share/codex-pet/opening/desktop_entry.py "$@"
EOF
chmod 755 "$stage/usr/bin/codex-pet-desktop-opening"
install -m 644 "$base/packaging/codex-pet.desktop" "$stage/usr/share/applications/"
dpkg-deb --build --root-owner-group "$stage" "$out/codex-pet_2.25.0_all.deb"
