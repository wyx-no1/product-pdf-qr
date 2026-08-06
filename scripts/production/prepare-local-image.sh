#!/bin/sh
set -eu

repository_root="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
registry_name="product-pdf-qr-test-registry"
registry_image="registry:2.8.3@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373"

if docker container inspect "$registry_name" >/dev/null 2>&1; then
  docker start "$registry_name" >/dev/null
else
  docker run --detach \
    --name "$registry_name" \
    --publish 127.0.0.1:5000:5000 \
    "$registry_image" >/dev/null
fi

build_and_publish() {
  environment_name="$1"
  target="$2"
  local_name="$3"
  repository="$4"
  source_tag="localhost:5000/$repository:issue34"
  first_tag="$local_name:issue34-first"
  second_tag="$local_name:issue34-second"

  docker build --pull --provenance=false --target "$target" --tag "$first_tag" "$repository_root"
  docker build --pull --provenance=false --target "$target" --tag "$second_tag" "$repository_root"

  first_id="$(docker image inspect "$first_tag" --format '{{.Id}}')"
  second_id="$(docker image inspect "$second_tag" --format '{{.Id}}')"
  if [ "$first_id" != "$second_id" ]; then
    echo "locked-input $target image builds were not reproducible" >&2
    exit 1
  fi

  docker tag "$second_tag" "$source_tag"
  docker push "$source_tag"
  digest="$(
    curl --fail --silent --show-error --head \
      --header 'Accept: application/vnd.oci.image.manifest.v1+json' \
      "http://127.0.0.1:5000/v2/$repository/manifests/issue34" |
      awk 'tolower($1) == "docker-content-digest:" {gsub("\r", "", $2); print $2}'
  )"
  immutable_reference="$source_tag@$digest"
  case "$immutable_reference" in
    *@sha256:????????????????????????????????????????????????????????????????) ;;
    *)
      echo "local registry did not return an immutable $target image" >&2
      exit 1
      ;;
  esac

  printf '%s=%s\n' "$environment_name" "$immutable_reference"
}

build_and_publish APP_IMAGE runtime product-pdf-qr-app product-pdf-qr/app
build_and_publish DB_IMAGE database-runtime product-pdf-qr-db product-pdf-qr/postgres
build_and_publish PROXY_IMAGE proxy-runtime product-pdf-qr-proxy product-pdf-qr/nginx
build_and_publish CERTBOT_IMAGE certbot-runtime product-pdf-qr-certbot product-pdf-qr/certbot
