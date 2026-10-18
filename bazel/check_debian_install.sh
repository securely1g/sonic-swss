#!/bin/bash
# Run only inside the disposable native Debian package-test container.
set -euo pipefail
if [ ! -f /.dockerenv ] || [ "${SONIC_DEB_TEST_CONTAINER:-}" != 1 ]; then
    echo 'Run only in the disposable Debian package-test container.' >&2
    exit 1
fi
package_directory=$(realpath "$1")
architecture=$(dpkg --print-architecture)
files=("$package_directory/swss_1.0.0_${architecture}.deb"
       "$package_directory/swss-dbg_1.0.0_${architecture}.deb")
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get -y --no-install-recommends install "$package_directory"/dependencies/*.deb "${files[@]}"
for package in swss swss-dbg; do
    test "$(dpkg-query -W -f='${Status}' "$package")" = 'install ok installed'
    test "$(dpkg-query -W -f='${Version}' "$package")" = 1.0.0
done
test "$(countersyncd --version)" = 'countersyncd 0.1.0'
while IFS= read -r binary; do
    if [ -f "$binary" ] && [ "$(head -c 4 "$binary")" = $'\177ELF' ]; then
        ldd "$binary" > "$package_directory/$(basename "$binary").ldd.txt"
        if grep -F 'not found' "$package_directory/$(basename "$binary").ldd.txt"; then exit 1; fi
    fi
done < <(dpkg-query -L swss | grep '^/usr/bin/')
dpkg-query -L swss-dbg | grep -E '/usr/lib/debug/.*\.debug$' > "$package_directory/debug-files.txt"
test -z "$(dpkg --audit)"
apt-get check
configuration=/etc/swss/config.d/netbouncer.json
test "$(stat -c %a "$configuration")" = 644
test "$(stat -c %a /usr/share/doc/swss/changelog.gz)" = 644
test "$(stat -c %a /usr/bin/orchagent)" = 755
dpkg-query -W -f='${Conffiles}' swss | grep -F "$configuration"
printf '\n' >> "$configuration"
before=$(sha256sum "$configuration")
apt-get -y --reinstall install "${files[@]}"
test "$(sha256sum "$configuration")" = "$before"
apt-get -y remove swss-dbg swss
test ! -e /usr/bin/orchagent
test ! -e /usr/bin/countersyncd
test -f "$configuration"
# Reinstall before the independent purge check: swss-dbg has no conffiles,
# so APT can drop this local-only package from its records after removal.
apt-get -y install "${files[@]}"
apt-get -y purge swss-dbg swss
test ! -e "$configuration"
test ! -e /usr/bin/orchagent
test ! -e /usr/bin/countersyncd
while IFS= read -r debug_file; do test ! -e "$debug_file"; done < "$package_directory/debug-files.txt"
test -z "$(dpkg --audit)"
apt-get check
printf '%s\n' '{"initial_configuration":"passed","installed_file_modes":"passed","runtime_loading":"passed","debug_files":"passed","reinstall":"passed","conffile_preservation":"passed","remove":"passed","purge":"passed","version_upgrade":"not tested: existing source packages use version 1.0.0"}' > "$package_directory/installation.json"
