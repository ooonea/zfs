#!/usr/bin/env bash
set -euo pipefail

# Qualification-only fixture for the two prepared ksh contributions.
source_revision=01ab4bc46ed7698effa81f73a597d754e948e689
qualified_tree=df8d2050d317d44d7a1e3ac95cabc166fc2e591c
patch_path=$(cd -- "$(dirname -- "$0")" && pwd)/qualified-ksh.patch
source_directory=$(mktemp -d /var/tmp/ksh-qualified.XXXXXXXX)
git -C "$source_directory" init
git -C "$source_directory" fetch --depth 1 https://github.com/ksh93/ksh.git "$source_revision"
git -C "$source_directory" checkout --detach FETCH_HEAD
test "$(git -C "$source_directory" rev-parse HEAD)" = "$source_revision"
git -C "$source_directory" apply --index "$patch_path"
test "$(git -C "$source_directory" write-tree)" = "$qualified_tree"
cd "$source_directory"
./bin/package make
./bin/package test > /var/tmp/qualified-ksh-tests.txt 2>&1
cat /var/tmp/qualified-ksh-tests.txt
grep -qx 'Total errors: 0' /var/tmp/qualified-ksh-tests.txt
# package host identifies the build directory on Linux and FreeBSD.
host_type=$(./bin/package host type)
binary="$source_directory/arch/$host_type/bin/ksh"
test -x "$binary"
case $(uname -s) in
  FreeBSD) destination=/usr/local/bin/ksh93 ;;
  Linux) destination=/usr/bin/ksh93 ;;
  *) exit 1 ;;
esac
sudo install -m 755 "$binary" "$destination"
sudo ln -sf "$destination" /bin/ksh
if [ "$(uname -s)" = FreeBSD ]; then
  sudo ln -sf "$destination" /usr/local/bin/ksh
fi
/bin/ksh -c 'print -r -- "$KSH_VERSION"'
