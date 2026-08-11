#!/bin/sh
# One-time, non-root ACL initialization for PR1's UID-10001-owned file volume.

set -eu

file_root="${BACKUP_SOURCE_ROOT:-/data/files}"

[ "$(id -u)" = "10001" ] || {
  echo "file access initialization must run as the PR1 file owner UID 10001" >&2
  exit 2
}
[ -d "$file_root" ] && [ ! -L "$file_root" ] || {
  echo "file access initialization requires a non-symlink file root" >&2
  exit 2
}
[ "$(stat -c '%u:%g' "$file_root")" = "10001:10001" ] || {
  echo "file access initialization refused: unexpected file-root owner" >&2
  exit 2
}
if find "$file_root" -xdev -type l -print -quit | grep -q .; then
  echo "file access initialization refused: symlink in file tree" >&2
  exit 2
fi
if find "$file_root" -xdev ! \( -user 10001 -o -user 10002 \) -print -quit | grep -q .; then
  echo "file access initialization refused: object has an unrelated owner" >&2
  exit 2
fi

# The backup profile mounts this tree read-only. The restore profile is the only
# UID-10002 job that mounts it read-write. Both named entries are required:
# restored objects are owned by UID 10002, while app objects are owned by UID
# 10001. The app and restore writers explicitly reopen the inherited ACL mask
# after secure 0600 creation.
find "$file_root" -xdev -user 10001 -type d -exec \
  setfacl -m u:10001:rwx,u:10002:rwx,d:u:10001:rwx,d:u:10002:rwx {} +
find "$file_root" -xdev -user 10001 -type f -exec \
  setfacl -m u:10001:rw-,u:10002:rw- {} +

probe="$file_root/.pr2a-acl-probe-$$"
umask 077
: >"$probe"
chmod 0660 "$probe"
setfacl -m u:10001:rw-,u:10002:rw- "$probe"
rm "$probe"

printf 'file-volume ACL initialized for backup/restore UID 10002\n'
