#!/bin/sh
# Full local synthetic age + pg_dump/pg_restore + isolated app rehearsal.

set -eu

repository_root="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
context="${PR2A_DOCKER_CONTEXT:-}"
case "$context" in
  pr2a-synthetic-*)
    ;;
  *)
    echo "rehearsal refused: PR2A_DOCKER_CONTEXT must start with pr2a-synthetic-" >&2
    exit 2
    ;;
esac
docker context inspect "$context" >/dev/null 2>&1 || {
  echo "rehearsal refused: named synthetic Docker context does not exist" >&2
  exit 2
}
case "$context" in
  *default* | *production* | *prod*)
    echo "rehearsal refused: default/production-like Docker context" >&2
    exit 2
    ;;
esac

run_id="pr2a-$(date -u '+%Y%m%dT%H%M%SZ')-$$"
resource_prefix="$(printf '%s' "$run_id" | tr '[:upper:]' '[:lower:]')"
synthetic_uid="$(id -u)"
synthetic_gid="$(id -g)"
[ "$synthetic_uid" -ne 0 ] || {
  echo "rehearsal refused: run as an unprivileged synthetic user" >&2
  exit 2
}
network="${resource_prefix}-internal"
database_container="${resource_prefix}-db"
app_container="${resource_prefix}-app"
backup_image="${resource_prefix}-backup"
app_image="${resource_prefix}-app-image"
state_access_volume="${resource_prefix}-state-access"
file_access_volume="${resource_prefix}-file-access"
work_root="$(mktemp -d "${TMPDIR:-/tmp}/product-pdf-qr-pr2a.XXXXXX")"
evidence_root="$repository_root/reports/backup-recovery/$run_id"
mkdir -p "$evidence_root"

cleanup() {
  docker --context "$context" rm --force "$app_container" "$database_container" \
    >/dev/null 2>&1 || true
  docker --context "$context" network rm "$network" >/dev/null 2>&1 || true
  docker --context "$context" volume rm "$state_access_volume" "$file_access_volume" \
    >/dev/null 2>&1 || true
  case "$work_root" in
    "${TMPDIR:-/tmp}"/product-pdf-qr-pr2a.*)
      rm -rf "$work_root"
      ;;
  esac
}
trap cleanup EXIT HUP INT TERM

record() {
  name="$1"
  shift
  started="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  set +e
  "$@" >"$evidence_root/$name.stdout" 2>"$evidence_root/$name.stderr"
  status=$?
  set -e
  completed="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  printf '{"command":"%s","started_at":"%s","completed_at":"%s","exit_code":%s}\n' \
    "$name" "$started" "$completed" "$status" >"$evidence_root/$name.json"
  [ "$status" -eq 0 ]
}

record_expected_failure() {
  name="$1"
  shift
  started="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  set +e
  "$@" >"$evidence_root/$name.stdout" 2>"$evidence_root/$name.stderr"
  status=$?
  set -e
  completed="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  printf '{"command":"%s","started_at":"%s","completed_at":"%s","exit_code":%s,"expected_failure":true}\n' \
    "$name" "$started" "$completed" "$status" >"$evidence_root/$name.json"
  [ "$status" -ne 0 ]
}

record build-backup env SOURCE_DATE_EPOCH=1754006400 BUILDKIT_MULTI_PLATFORM=1 \
  docker --context "$context" build --pull \
  --provenance=false --target backup-recovery-runtime \
  --tag "$backup_image" "$repository_root"
record build-app docker --context "$context" build --pull \
  --provenance=false --target runtime --tag "$app_image" "$repository_root"
record create-network docker --context "$context" network create --internal "$network"
record create-state-access-volume docker --context "$context" volume create "$state_access_volume"
record state-volume-copyup docker --context "$context" run --rm \
  --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --volume "$state_access_volume:/var/lib/backup" --entrypoint sh "$backup_image" \
  -eu -c 'test "$(stat -c "%u:%g:%a" /var/lib/backup)" = "10002:10002:700";
    : >/var/lib/backup/non-root-write-probe'

record create-file-access-volume docker --context "$context" volume create "$file_access_volume"
record create-private-file-tree docker --context "$context" run --rm \
  --user 10001:10001 --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --volume "$file_access_volume:/data/files" --entrypoint sh "$app_image" \
  -eu -c 'umask 077; mkdir -p /data/files/files/private;
    printf before >/data/files/files/private/before.pdf;
    python -c "import os, tempfile
descriptor, path = tempfile.mkstemp(dir=\"/data/files/files/private\")
os.fchmod(descriptor, 0o660)
os.write(descriptor, b\"acl-required\")
os.close(descriptor)
os.replace(path, \"/data/files/files/private/acl-required.pdf\")";
    test "$(stat -c "%a" /data/files/files/private)" = "700";
    test "$(stat -c "%a" /data/files/files/private/before.pdf)" = "600";
    test "$(stat -c "%a" /data/files/files/private/acl-required.pdf)" = "660"'
record_expected_failure unconfigured-acl-backup-uid-denied docker --context "$context" run --rm \
  --user 10002:10002 --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --network none \
  --volume "$file_access_volume:/data/files:ro" --entrypoint cat "$backup_image" \
  /data/files/files/private/acl-required.pdf
record_expected_failure unconfigured-acl-unrelated-uid-denied docker --context "$context" run --rm \
  --user 10003:10003 --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --network none \
  --volume "$file_access_volume:/data/files:ro" --entrypoint cat "$backup_image" \
  /data/files/files/private/acl-required.pdf
record initialize-file-acl docker --context "$context" run --rm \
  --user 10001:10001 --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --network none \
  --volume "$file_access_volume:/data/files" --entrypoint sh "$backup_image" \
  /opt/backup-recovery/scripts/backup_recovery/init-file-access.sh
record create-private-file-after-acl docker --context "$context" run --rm \
  --user 10001:10001 --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --volume "$file_access_volume:/data/files" --entrypoint python "$app_image" \
  -c 'import os, tempfile
os.umask(0o077)
directory = "/data/files/files/private/after"
os.makedirs(directory, exist_ok=True)
descriptor, temporary = tempfile.mkstemp(prefix="upload-", suffix=".part", dir=directory)
os.fchmod(descriptor, 0o660)
os.write(descriptor, b"after")
os.fsync(descriptor)
os.close(descriptor)
os.replace(temporary, f"{directory}/after.pdf")'
record create-qrcode-after-acl docker --context "$context" run --rm \
  --user 10001:10001 --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --network none \
  --volume "$file_access_volume:/data/files" --entrypoint python "$app_image" \
  -c 'import asyncio
from pathlib import Path
from product_pdf_qr.domains.qrcode import QRCodeService
result = asyncio.run(QRCodeService(Path("/data/files"), "https://example.invalid").get_or_generate(
    "A001", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
))
assert not result.cache_hit and result.cache_error is None'
record uid-10002-file-access docker --context "$context" run --rm \
  --user 10002:10002 --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --volume "$file_access_volume:/data/files" --entrypoint sh "$backup_image" \
  -eu -c 'test "$(cat /data/files/files/private/before.pdf)" = before;
    test "$(cat /data/files/files/private/acl-required.pdf)" = acl-required;
    test "$(cat /data/files/files/private/after/after.pdf)" = after;
    test -s /data/files/qrcodes/A001.png;
    : >/data/files/files/private/restore-write-probe'
record uid-10002-qrcode-inventory docker --context "$context" run --rm \
  --user 10002:10002 --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --network none \
  --volume "$file_access_volume:/data/files:ro" --entrypoint python "$backup_image" \
  -c 'from pathlib import Path
from scripts.backup_recovery.model import inventory
assert any(item["path"] == "qrcodes/A001.png" for item in inventory(Path("/data/files")))'
record uid-10002-actual-restore docker --context "$context" run --rm \
  --user 10002:10002 --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --network none \
  --volume "$file_access_volume:/data/files" --entrypoint sh "$backup_image" \
  -eu -c 'umask 077
    age-keygen -o /data/files/.acl-identity 2>/data/files/.acl-recipient
    recipient="$(sed -n "s/^Public key: //p" /data/files/.acl-recipient)"
    printf restored-by-uid-10002 |
      age --encrypt --recipient "$recipient" > /data/files/.acl-cipher.age
    python -c "from pathlib import Path
from scripts.backup_recovery.crypto import decrypt_to_path
decrypt_to_path(
    Path(\"/data/files/.acl-cipher.age\"),
    Path(\"/data/files/.acl-identity\"),
    Path(\"/data/files/files/restored/nested/restored.pdf\"),
)"
    rm /data/files/.acl-identity /data/files/.acl-recipient /data/files/.acl-cipher.age'
record uid-10001-restored-file-access docker --context "$context" run --rm \
  --user 10001:10001 --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --network none \
  --volume "$file_access_volume:/data/files:ro" --entrypoint sh "$app_image" \
  -eu -c 'test "$(cat /data/files/files/restored/nested/restored.pdf)" = restored-by-uid-10002'
record_expected_failure unrelated-uid-file-access docker --context "$context" run --rm \
  --user 10003:10003 --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --volume "$file_access_volume:/data/files:ro" --entrypoint sh "$backup_image" \
  -eu -c 'cat /data/files/files/private/before.pdf'
record_expected_failure unrelated-uid-qrcode-access docker --context "$context" run --rm \
  --user 10003:10003 --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --network none \
  --volume "$file_access_volume:/data/files:ro" --entrypoint cat "$backup_image" \
  /data/files/qrcodes/A001.png
record file-acl-evidence docker --context "$context" run --rm \
  --user 10001:10001 --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --volume "$file_access_volume:/data/files:ro" --entrypoint getfacl "$backup_image" \
  -p /data/files/files/private/after/after.pdf

mkdir -p "$work_root/keys" "$work_root/source" "$work_root/target" \
  "$work_root/remote" "$work_root/state" "$work_root/secrets"
chmod 0700 "$work_root/keys" "$work_root/source" "$work_root/target" \
  "$work_root/remote" "$work_root/state" "$work_root/secrets"
printf 'synthetic-v1' >"$work_root/v1.pdf"
printf 'synthetic-v2' >"$work_root/v2.pdf"
v1_sha="$(shasum -a 256 "$work_root/v1.pdf" | awk '{print $1}')"
v2_sha="$(shasum -a 256 "$work_root/v2.pdf" | awk '{print $1}')"
v1_relative="$(printf '%s' "$v1_sha" | cut -c 1-2)/$(printf '%s' "$v1_sha" | cut -c 3-4)/$v1_sha.pdf"
v2_relative="$(printf '%s' "$v2_sha" | cut -c 1-2)/$(printf '%s' "$v2_sha" | cut -c 3-4)/$v2_sha.pdf"
mkdir -p "$work_root/source/files/$(dirname "$v1_relative")" \
  "$work_root/source/files/$(dirname "$v2_relative")"
mv "$work_root/v1.pdf" "$work_root/source/files/$v1_relative"
mv "$work_root/v2.pdf" "$work_root/source/files/$v2_relative"
printf 'post-backup-append-only' >"$work_root/target/post-backup-extra.pdf"
find "$work_root/source" "$work_root/target" -type d -exec chmod 0700 {} +
find "$work_root/source" "$work_root/target" -type f -exec chmod 0600 {} +

record generate-key docker --context "$context" run --rm \
  --user "$synthetic_uid:$synthetic_gid" \
  --volume "$work_root/keys:/keys" --entrypoint sh "$backup_image" \
  -eu -c 'umask 077
    age-keygen -o /keys/identity.txt 2>/keys/recipient.log
    printf 0123456789abcdef0123456789abcdef > /keys/manifest-authentication.key
    printf fedcba9876543210fedcba9876543210 > /keys/restore-verification-authentication.key
    python -c "from pathlib import Path; from cryptography.hazmat.primitives import serialization; from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey; private = Ed25519PrivateKey.from_private_bytes(Path(\"/keys/manifest-authentication.key\").read_bytes()); Path(\"/keys/manifest-verification.key\").write_bytes(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)); restore_private = Ed25519PrivateKey.from_private_bytes(Path(\"/keys/restore-verification-authentication.key\").read_bytes()); Path(\"/keys/restore-verification.key\").write_bytes(restore_private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))"'
recipient="$(sed -n 's/^Public key: //p' "$work_root/keys/recipient.log")"
[ -n "$recipient" ]

record start-db docker --context "$context" run --detach \
  --name "$database_container" --network "$network" --network-alias db \
  --env POSTGRES_HOST_AUTH_METHOD=trust \
  postgres:16.14-alpine3.24@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777
attempts=0
until docker --context "$context" exec "$database_container" pg_isready -U postgres \
  >/dev/null 2>&1; do
  attempts=$((attempts + 1))
  [ "$attempts" -lt 60 ] || {
    echo "synthetic PostgreSQL readiness timeout" >&2
    exit 1
  }
  sleep 1
done

docker --context "$context" exec "$database_container" psql -U postgres \
  -v ON_ERROR_STOP=1 -c "
CREATE ROLE app_migrate LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE app_backup LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE app_rw LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
" >"$evidence_root/database-bootstrap.stdout"
docker --context "$context" exec "$database_container" createdb -U postgres \
  --owner app_migrate source
docker --context "$context" exec "$database_container" createdb -U postgres \
  --owner app_migrate restore

docker --context "$context" exec "$database_container" psql -U app_migrate -d source \
  -v ON_ERROR_STOP=1 -c "
CREATE TABLE admins (
 id bigserial PRIMARY KEY, username varchar(64), password_hash text,
 must_change_password boolean, password_updated_at timestamptz,
 last_login_at timestamptz, created_at timestamptz);
CREATE TABLE products (
 id bigserial PRIMARY KEY, code varchar(64), public_token varchar(26),
 status varchar(16), current_version_id bigint, name varchar(120),
 created_at timestamptz, updated_at timestamptz);
CREATE TABLE pdf_files (
 id bigserial PRIMARY KEY, sha256 char(64), size_bytes bigint,
 storage_path text, created_at timestamptz);
CREATE TABLE pdf_versions (
 id bigserial PRIMARY KEY, product_id bigint, pdf_file_id bigint,
 version_no integer, original_filename text, uploaded_by bigint,
 uploaded_at timestamptz);
CREATE TABLE admin_sessions (
 id uuid PRIMARY KEY, admin_id bigint, token_hash char(64),
 issued_at timestamptz, expires_at timestamptz, revoked_at timestamptz);
CREATE TABLE audit_events (
 id bigserial PRIMARY KEY, occurred_at timestamptz, actor_type varchar(16),
 actor_id bigint, action varchar(48), target_type varchar(24), target_id bigint,
 product_code varchar(64), result varchar(16), request_id uuid, detail jsonb);
CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY);
INSERT INTO alembic_version VALUES ('20260804_0002');
INSERT INTO admins VALUES
 (1,'synthetic','unused',false,now(),NULL,now());
INSERT INTO products VALUES
 (1,'NOFILE','00000000000000000000000001','active',NULL,'No file',now(),now()),
 (2,'ACTIVE','00000000000000000000000002','active',2,'Active',now(),now()),
 (3,'DISABLED','00000000000000000000000003','disabled',3,'Disabled',now(),now());
INSERT INTO pdf_files VALUES
 (1,'$v1_sha',12,'$v1_relative',now()),
 (2,'$v2_sha',12,'$v2_relative',now());
INSERT INTO pdf_versions VALUES
 (1,2,1,1,'v1.pdf',1,now()), (2,2,2,2,'v2.pdf',1,now()),
 (3,3,2,1,'disabled.pdf',1,now());
INSERT INTO audit_events VALUES
 (1,now(),'admin',1,'upload','product',2,'ACTIVE','success',NULL,'{}');
REVOKE ALL ON audit_events FROM app_rw;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_backup;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO app_backup;
GRANT USAGE ON SCHEMA public TO app_backup,app_rw;
GRANT SELECT ON products,pdf_versions,pdf_files,audit_events TO app_rw;
GRANT UPDATE(status,current_version_id,updated_at) ON products TO app_rw;
" >"$evidence_root/fixture.stdout"

commit_sha="$(git -C "$repository_root" rev-parse HEAD)"
image_ref="synthetic:v1@sha256:1111111111111111111111111111111111111111111111111111111111111111"
common_backup_args="
--user $synthetic_uid:$synthetic_gid
--network $network
--volume $work_root/source:/data/files:ro
--volume $work_root/remote:/remote
--volume $work_root/state:/var/lib/backup
--volume $repository_root:/source:ro
--volume $work_root/keys/manifest-authentication.key:/run/secrets/manifest-authentication.key:ro
--env BACKUP_CONTRACT=/source/deploy/backup/contract.json
--env BACKUP_REMOTE=local:/remote
--env BACKUP_SYNTHETIC=1
--env BACKUP_REMOTE_PREFIX=synthetic
--env BACKUP_AGE_RECIPIENT=$recipient
--env BACKUP_RECIPIENT_KEY_ID=synthetic-key
--env BACKUP_MANIFEST_AUTHENTICATION_KEY=/run/secrets/manifest-authentication.key
--env BACKUP_MANIFEST_AUTHENTICATION_KEY_ID=synthetic-manifest-auth
--env BACKUP_SOURCE_ROOT=/data/files
--env BACKUP_REPOSITORY_ROOT=/source
--env BACKUP_STATE_ROOT=/var/lib/backup
--env PGHOST=db
--env PGDATABASE=source
--env PGUSER=app_backup
--env SOURCE_COMMIT=$commit_sha
--env APP_IMAGE=$image_ref
--env DB_IMAGE=$image_ref
--env PROXY_IMAGE=$image_ref
--env CERTBOT_IMAGE=$image_ref
--env BACKUP_IMAGE=$image_ref
"
# shellcheck disable=SC2086
record precopy docker --context "$context" run --rm $common_backup_args "$backup_image" precopy
# shellcheck disable=SC2086
record finalize docker --context "$context" run --rm $common_backup_args "$backup_image" finalize
backup_id="$(uv run python -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["backup_id"])' \
  "$work_root/state/last-success.json")"
success_state_sha="$(shasum -a 256 "$work_root/state/last-success.json" | awk '{print $1}')"
completion_count="$(find "$work_root/remote/synthetic/complete" -type f | wc -l | tr -d ' ')"
for failure_stage in files dump manifest encryption upload; do
  # shellcheck disable=SC2086
  record_expected_failure "backup-failure-$failure_stage" \
    docker --context "$context" run --rm $common_backup_args \
    --env "BACKUP_FAIL_STAGE=$failure_stage" "$backup_image" finalize
  test "$(shasum -a 256 "$work_root/state/last-success.json" | awk '{print $1}')" \
    = "$success_state_sha"
  test "$(find "$work_root/remote/synthetic/complete" -type f | wc -l | tr -d ' ')" \
    = "$completion_count"
done

manifest_key="$(uv run python -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["manifest_key"])' \
  "$work_root/remote/synthetic/complete/$backup_id.json")"
manifest_cipher="$work_root/remote/$manifest_key"
record generate-wrong-key docker --context "$context" run --rm \
  --user "$synthetic_uid:$synthetic_gid" \
  --volume "$work_root/keys:/keys" --entrypoint sh "$backup_image" \
  -eu -c 'umask 077; age-keygen -o /keys/wrong-identity.txt 2>/keys/wrong-recipient.log'
record_expected_failure wrong-key docker --context "$context" run --rm \
  --volume "$manifest_cipher:/manifest.age:ro" \
  --volume "$work_root/keys/wrong-identity.txt:/wrong-key:ro" \
  --entrypoint age "$backup_image" --decrypt --identity /wrong-key /manifest.age
cipher_size="$(wc -c <"$manifest_cipher" | tr -d ' ')"
dd if="$manifest_cipher" of="$work_root/keys/truncated.age" bs=1 \
  count=$((cipher_size - 1)) 2>/dev/null
record_expected_failure truncated-ciphertext docker --context "$context" run --rm \
  --volume "$work_root/keys/truncated.age:/manifest.age:ro" \
  --volume "$work_root/keys/identity.txt:/identity:ro" \
  --entrypoint age "$backup_image" --decrypt --identity /identity /manifest.age
cp "$manifest_cipher" "$work_root/keys/tampered.age"
uv run python -c \
  'from pathlib import Path; import sys; p=Path(sys.argv[1]); b=bytearray(p.read_bytes()); b[len(b)//2]^=1; p.write_bytes(b)' \
  "$work_root/keys/tampered.age"
record_expected_failure tampered-ciphertext docker --context "$context" run --rm \
  --volume "$work_root/keys/tampered.age:/manifest.age:ro" \
  --volume "$work_root/keys/identity.txt:/identity:ro" \
  --entrypoint age "$backup_image" --decrypt --identity /identity /manifest.age
record_expected_failure remote-plaintext-canary \
  grep -R -a -F 'synthetic-v1' "$work_root/remote"

expires_at="$(uv run python -c \
  'from datetime import UTC,datetime,timedelta; print((datetime.now(UTC)+timedelta(hours=1)).isoformat())')"
printf '%s\n' 'synthetic-environment-01' >"$work_root/secrets/environment-id"
printf '%s\n' "{
  \"environment_id\":\"synthetic-environment-01\",
  \"backup_id\":\"$backup_id\",
  \"operator_id\":\"synthetic-operator\",
  \"approved_data_loss_window\":\"synthetic-only\",
  \"authorization_record\":\"synthetic-change-1\",
  \"expires_at\":\"$expires_at\",
  \"one_time_challenge\":\"synthetic-bound-challenge\"
}" >"$work_root/secrets/authorization.json"
chmod 0600 "$work_root/secrets/environment-id" "$work_root/secrets/authorization.json"

common_restore_args="
--user $synthetic_uid:$synthetic_gid
--network $network
--volume $work_root/target:/data/files
--volume $work_root/remote:/remote
--volume $work_root/state:/var/lib/backup
--volume $repository_root:/source:ro
--volume $work_root/keys/identity.txt:/run/secrets/age-identity.txt:ro
--volume $work_root/keys/manifest-verification.key:/run/secrets/manifest-verification.key:ro
--volume $work_root/keys/restore-verification-authentication.key:/run/secrets/restore-verification-authentication.key:ro
--volume $work_root/secrets/environment-id:/run/config/environment-id:ro
--volume $work_root/secrets/authorization.json:/run/secrets/restore-authorization.json:ro
--env BACKUP_CONTRACT=/source/deploy/backup/contract.json
--env BACKUP_REMOTE=local:/remote
--env BACKUP_SYNTHETIC=1
--env RESTORE_SYNTHETIC_BIND_MOUNT=1
--env BACKUP_REMOTE_PREFIX=synthetic
--env BACKUP_AGE_RECIPIENT=$recipient
--env BACKUP_RECIPIENT_KEY_ID=synthetic-key
--env RESTORE_MANIFEST_VERIFICATION_KEY=/run/secrets/manifest-verification.key
--env BACKUP_MANIFEST_AUTHENTICATION_KEY_ID=synthetic-manifest-auth
--env RESTORE_VERIFICATION_AUTHENTICATION_KEY=/run/secrets/restore-verification-authentication.key
--env RESTORE_VERIFICATION_AUTHENTICATION_KEY_ID=synthetic-restore-verification
--env BACKUP_SOURCE_ROOT=/data/files
--env BACKUP_REPOSITORY_ROOT=/source
--env BACKUP_STATE_ROOT=/var/lib/backup
--env RESTORE_AGE_IDENTITY=/run/secrets/age-identity.txt
--env RESTORE_ENVIRONMENT_ID=synthetic-environment-01
--env RESTORE_ENVIRONMENT_MARKER=/run/config/environment-id
--env RESTORE_AUTHORIZATION=/run/secrets/restore-authorization.json
--env RESTORE_CONFIRMATION=synthetic-bound-challenge
--env PGHOST=db
--env PGDATABASE=restore
--env PGUSER=app_migrate
--env SOURCE_COMMIT=$commit_sha
--env APP_IMAGE=$image_ref
--env DB_IMAGE=$image_ref
--env PROXY_IMAGE=$image_ref
--env CERTBOT_IMAGE=$image_ref
--env BACKUP_IMAGE=$image_ref
"
for stage in declare preflight retain-site restore-database restore-files offline-validate; do
  # shellcheck disable=SC2086
  record "restore-$stage" docker --context "$context" run --rm \
    $common_restore_args "$backup_image" "$stage" --backup-id "$backup_id"
done
test -f "$work_root/target/post-backup-extra.pdf"
record post-restore-app-backup-access docker --context "$context" run --rm \
  --user "$synthetic_uid:$synthetic_gid" --network "$network" \
  --env PGHOST=db --env PGDATABASE=restore --env PGUSER=app_backup \
  --entrypoint pg_dump "$backup_image" --schema-only --no-owner

record start-app docker --context "$context" run --detach \
  --name "$app_container" --network "$network" \
  --user 10001:10001 --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m,uid=10001,gid=10001 \
  --volume "$work_root/target:/data/files" \
  --env DATABASE_URL=postgresql://app_rw@db:5432/restore \
  --env STORAGE_ROOT=/data/files --env APP_BIND_HOST=0.0.0.0 \
  --env APP_PORT=8000 --env SESSION_COOKIE_SECURE=false \
  "$app_image"
attempts=0
until docker --context "$context" run --rm --network "$network" \
  --entrypoint python "$backup_image" -c \
  "import urllib.request; urllib.request.urlopen('http://$app_container:8000/health/ready',timeout=2)" \
  >/dev/null 2>&1; do
  attempts=$((attempts + 1))
  [ "$attempts" -lt 60 ] || {
    echo "isolated app readiness timeout" >&2
    exit 1
  }
  sleep 1
done

# The checker talks only over the internal network; no proxy or host port exists.
record isolated-functional docker --context "$context" run --rm \
  --network "$network" \
  --entrypoint python \
  --volume "$work_root/secrets:/evidence" \
  --env PGHOST=db --env PGDATABASE=restore \
  "$backup_image" -m scripts.backup_recovery.synthetic_functional_check \
  --base-url "http://$app_container:8000" --backup-id "$backup_id" \
  --output /evidence/functional.json
# shellcheck disable=SC2086
record functional-gate docker --context "$context" run --rm \
  $common_restore_args --volume "$work_root/secrets/functional.json:/evidence.json:ro" \
  "$backup_image" record-functional-validation --backup-id "$backup_id" \
  --evidence /evidence.json
# shellcheck disable=SC2086
record proxy-gate docker --context "$context" run --rm \
  $common_restore_args "$backup_image" authorize-proxy --backup-id "$backup_id"
# Synthetic harness has no proxy/public port; external-ready exercises verified
# generation after the proxy-last gate, while real readiness remains authorized.
# shellcheck disable=SC2086
record verified-gate docker --context "$context" run --rm \
  $common_restore_args "$backup_image" external-ready --backup-id "$backup_id"

test -f "$work_root/remote/synthetic/verified/$backup_id.json"
test -f "$work_root/target/files/$v1_relative"
test -f "$work_root/target/files/$v2_relative"
test -f "$work_root/target/post-backup-extra.pdf"
docker --context "$context" rm --force "$app_container" >/dev/null

for failure_stage in decrypt site_retention database files offline_validation \
  isolated_functional_validation proxy; do
  failure_root="$work_root/failure-$failure_stage"
  mkdir -p "$failure_root/state" "$failure_root/target"
  chmod 0700 "$failure_root/state" "$failure_root/target"
  printf 'preserve-me' >"$failure_root/target/post-backup-extra.pdf"
  failure_restore_args="
  --user $synthetic_uid:$synthetic_gid
  --network $network
  --volume $failure_root/target:/data/files
  --volume $work_root/remote:/remote
  --volume $failure_root/state:/var/lib/backup
  --volume $repository_root:/source:ro
  --volume $work_root/keys/identity.txt:/run/secrets/age-identity.txt:ro
  --volume $work_root/keys/manifest-verification.key:/run/secrets/manifest-verification.key:ro
  --volume $work_root/keys/restore-verification-authentication.key:/run/secrets/restore-verification-authentication.key:ro
  --volume $work_root/secrets/environment-id:/run/config/environment-id:ro
  --volume $work_root/secrets/authorization.json:/run/secrets/restore-authorization.json:ro
  --env BACKUP_CONTRACT=/source/deploy/backup/contract.json
  --env BACKUP_REMOTE=local:/remote
  --env BACKUP_SYNTHETIC=1
  --env RESTORE_SYNTHETIC_BIND_MOUNT=1
  --env BACKUP_REMOTE_PREFIX=synthetic
  --env BACKUP_AGE_RECIPIENT=$recipient
  --env BACKUP_RECIPIENT_KEY_ID=synthetic-key
  --env RESTORE_MANIFEST_VERIFICATION_KEY=/run/secrets/manifest-verification.key
  --env BACKUP_MANIFEST_AUTHENTICATION_KEY_ID=synthetic-manifest-auth
  --env RESTORE_VERIFICATION_AUTHENTICATION_KEY=/run/secrets/restore-verification-authentication.key
  --env RESTORE_VERIFICATION_AUTHENTICATION_KEY_ID=synthetic-restore-verification
  --env BACKUP_SOURCE_ROOT=/data/files
  --env BACKUP_REPOSITORY_ROOT=/source
  --env BACKUP_STATE_ROOT=/var/lib/backup
  --env RESTORE_AGE_IDENTITY=/run/secrets/age-identity.txt
  --env RESTORE_ENVIRONMENT_ID=synthetic-environment-01
  --env RESTORE_ENVIRONMENT_MARKER=/run/config/environment-id
  --env RESTORE_AUTHORIZATION=/run/secrets/restore-authorization.json
  --env RESTORE_CONFIRMATION=synthetic-bound-challenge
  --env PGHOST=db
  --env PGDATABASE=restore
  --env PGUSER=app_migrate
  --env SOURCE_COMMIT=$commit_sha
  --env APP_IMAGE=$image_ref
  --env DB_IMAGE=$image_ref
  --env PROXY_IMAGE=$image_ref
  --env CERTBOT_IMAGE=$image_ref
  --env BACKUP_IMAGE=$image_ref
  "
  if [ "$failure_stage" = "decrypt" ]; then
    # shellcheck disable=SC2086
    record_expected_failure "restore-failure-$failure_stage" \
      docker --context "$context" run --rm $failure_restore_args \
      --env RESTORE_FAIL_STAGE=decrypt "$backup_image" preflight --backup-id "$backup_id"
    continue
  fi
  # shellcheck disable=SC2086
  record "failure-$failure_stage-preflight" docker --context "$context" run --rm \
    $failure_restore_args "$backup_image" preflight --backup-id "$backup_id"
  if [ "$failure_stage" = "site_retention" ]; then
    # shellcheck disable=SC2086
    record_expected_failure "restore-failure-$failure_stage" \
      docker --context "$context" run --rm $failure_restore_args \
      --env RESTORE_FAIL_STAGE=site_retention "$backup_image" retain-site \
      --backup-id "$backup_id"
    checkpoint="$(
      find "$failure_root/state/restores/$backup_id" -maxdepth 1 -type f \
        -name '*.json' -print -quit
    )"
    operation_id="$(basename "$checkpoint" .json)"
    staging="$failure_root/state/site-retention/$backup_id/.$operation_id.staging"
    mkdir -p "$staging"
    printf partial >"$staging/interrupted.age"
    # shellcheck disable=SC2086
    record "restore-site-retention-retry" docker --context "$context" run --rm \
      $failure_restore_args "$backup_image" retain-site --backup-id "$backup_id"
    test ! -e "$staging"
    test -f \
      "$failure_root/state/site-retention/$backup_id/$operation_id/database.dump.age"
    continue
  fi
  # shellcheck disable=SC2086
  record "failure-$failure_stage-retain" docker --context "$context" run --rm \
    $failure_restore_args "$backup_image" retain-site --backup-id "$backup_id"
  if [ "$failure_stage" = "database" ]; then
    # shellcheck disable=SC2086
    record_expected_failure "restore-failure-$failure_stage" \
      docker --context "$context" run --rm $failure_restore_args \
      --env RESTORE_FAIL_STAGE=database "$backup_image" restore-database \
      --backup-id "$backup_id"
    continue
  fi
  # shellcheck disable=SC2086
  record "failure-$failure_stage-database" docker --context "$context" run --rm \
    $failure_restore_args "$backup_image" restore-database --backup-id "$backup_id"
  if [ "$failure_stage" = "files" ]; then
    # shellcheck disable=SC2086
    record_expected_failure "restore-failure-$failure_stage" \
      docker --context "$context" run --rm $failure_restore_args \
      --env RESTORE_FAIL_STAGE=files "$backup_image" restore-files \
      --backup-id "$backup_id"
    # The app/offline gate cannot advance while files are incomplete.
    # shellcheck disable=SC2086
    record_expected_failure "restore-files-incomplete-gate" \
      docker --context "$context" run --rm $failure_restore_args \
      "$backup_image" offline-validate --backup-id "$backup_id"
    test -f "$failure_root/target/post-backup-extra.pdf"
    # shellcheck disable=SC2086
    record "restore-files-retry-preflight" \
      docker --context "$context" run --rm $failure_restore_args \
      "$backup_image" preflight --backup-id "$backup_id"
    # shellcheck disable=SC2086
    record "restore-files-retry-retain" docker --context "$context" run --rm \
      $failure_restore_args "$backup_image" retain-site --backup-id "$backup_id"
    # shellcheck disable=SC2086
    record "restore-files-retry-database" docker --context "$context" run --rm \
      $failure_restore_args "$backup_image" restore-database --backup-id "$backup_id"
    # shellcheck disable=SC2086
    record "restore-files-retry-files" docker --context "$context" run --rm \
      $failure_restore_args "$backup_image" restore-files --backup-id "$backup_id"
    # shellcheck disable=SC2086
    record "restore-files-retry-offline" docker --context "$context" run --rm \
      $failure_restore_args "$backup_image" offline-validate --backup-id "$backup_id"
    continue
  fi
  # shellcheck disable=SC2086
  record "failure-$failure_stage-files" docker --context "$context" run --rm \
    $failure_restore_args "$backup_image" restore-files --backup-id "$backup_id"
  if [ "$failure_stage" = "offline_validation" ]; then
    # shellcheck disable=SC2086
    record_expected_failure "restore-failure-$failure_stage" \
      docker --context "$context" run --rm $failure_restore_args \
      --env RESTORE_FAIL_STAGE=offline_validation "$backup_image" offline-validate \
      --backup-id "$backup_id"
    # Replay the production wrapper's completed prefix. Completed destructive
    # stages must be idempotent while the failed validation stage is retried.
    # shellcheck disable=SC2086
    record "restore-offline-retry-preflight" docker --context "$context" run --rm \
      $failure_restore_args "$backup_image" preflight --backup-id "$backup_id"
    # shellcheck disable=SC2086
    record "restore-offline-retry-retain" docker --context "$context" run --rm \
      $failure_restore_args "$backup_image" retain-site --backup-id "$backup_id"
    # shellcheck disable=SC2086
    record "restore-offline-retry-database" docker --context "$context" run --rm \
      $failure_restore_args "$backup_image" restore-database --backup-id "$backup_id"
    # shellcheck disable=SC2086
    record "restore-offline-retry-files" docker --context "$context" run --rm \
      $failure_restore_args "$backup_image" restore-files --backup-id "$backup_id"
    # shellcheck disable=SC2086
    record "restore-offline-retry-offline" docker --context "$context" run --rm \
      $failure_restore_args "$backup_image" offline-validate --backup-id "$backup_id"
    continue
  fi
  # shellcheck disable=SC2086
  record "failure-$failure_stage-offline" docker --context "$context" run --rm \
    $failure_restore_args "$backup_image" offline-validate --backup-id "$backup_id"
  if [ "$failure_stage" = "isolated_functional_validation" ]; then
    # shellcheck disable=SC2086
    record_expected_failure "restore-failure-$failure_stage" \
      docker --context "$context" run --rm $failure_restore_args \
      --volume "$work_root/secrets/functional.json:/evidence.json:ro" \
      --env RESTORE_FAIL_STAGE=isolated_functional_validation \
      "$backup_image" record-functional-validation --backup-id "$backup_id" \
      --evidence /evidence.json
    continue
  fi
  # shellcheck disable=SC2086
  record "failure-$failure_stage-functional" docker --context "$context" run --rm \
    $failure_restore_args \
    --volume "$work_root/secrets/functional.json:/evidence.json:ro" \
    "$backup_image" record-functional-validation --backup-id "$backup_id" \
    --evidence /evidence.json
  # shellcheck disable=SC2086
  record_expected_failure "restore-failure-$failure_stage" \
    docker --context "$context" run --rm $failure_restore_args \
    --env RESTORE_FAIL_STAGE=proxy "$backup_image" authorize-proxy \
    --backup-id "$backup_id"
done

record image-user docker --context "$context" run --rm --entrypoint id "$backup_image" -u
grep -qx '10002' "$evidence_root/image-user.stdout"
record tool-versions docker --context "$context" run --rm --entrypoint sh "$backup_image" \
  -c 'age --version; pg_dump --version; rclone version | sed -n "1p"'
record image-content docker --context "$context" run --rm --user 0:0 --entrypoint sh \
  "$backup_image" -c \
  'test -z "$(find /opt/backup-recovery -type f \( -name "*.pem" -o -name "*.key" -o -name ".env*" \) -print -quit)"; ! find /opt/backup-recovery -type d \( -name pytest -o -name mypy -o -name __pycache__ \) | grep .'

commit="$(git -C "$repository_root" rev-parse HEAD)"
image_id="$(docker --context "$context" image inspect "$backup_image" --format '{{.Id}}')"
printf '{"run_id":"%s","scope":"local_isolated_synthetic","commit_sha":"%s","backup_id":"%s","backup_image_id":"%s","result":"PASS","production_rto_rpo_claim":false}\n' \
  "$run_id" "$commit" "$backup_id" "$image_id" >"$evidence_root/evidence-index.json"
printf 'local synthetic rehearsal PASS: %s\n' "$evidence_root"
