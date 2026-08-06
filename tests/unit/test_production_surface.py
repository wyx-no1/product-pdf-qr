"""Mechanical checks for the production-only runtime surface."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from product_pdf_qr.config import PRODUCTION_APP_IP, PRODUCTION_PROXY_IP, Settings
from scripts.production.validate_compose import (
    validate_app_boundary_environment,
    validate_image_reference,
)

ROOT = Path(__file__).resolve().parents[2]
DATABASE_URL = "postgresql://app_rw:synthetic@db:5432/product_pdf_qr"
NGINX_MAIN = ROOT / "deploy/production/nginx/nginx.conf"
NGINX_SITE = ROOT / "deploy/production/nginx/templates/site.conf.template"
DOCKERFILE = ROOT / "Dockerfile"


def production_settings(**overrides: object) -> Settings:
    """Build one strict production configuration without reading process state."""

    values: dict[str, object] = {
        "deployment_mode": "production",
        "database_url": DATABASE_URL,
        "app_bind_host": "172.30.0.20",
        "forwarded_allow_ips": PRODUCTION_PROXY_IP,
        "public_domain": "qr.example.test",
        "public_base_url": "https://qr.example.test",
    }
    values.update(overrides)
    return Settings.model_validate(values)


def test_production_origin_and_proxy_trust_accept_exact_contract() -> None:
    settings = production_settings()

    assert settings.public_base_url == "https://qr.example.test"
    assert settings.public_domain == "qr.example.test"
    assert settings.app_bind_host == PRODUCTION_APP_IP
    assert settings.forwarded_allow_ips == "172.30.0.10"


@pytest.mark.parametrize(
    ("domain", "url"),
    [
        ("qr.example.test", ""),
        ("qr.example.test", "http://qr.example.test"),
        ("qr.example.test", "https://example.com"),
        ("qr.example.test", "https://user@qr.example.test"),
        ("qr.example.test", "https://qr.example.test:443"),
        ("qr.example.test", "https://qr.example.test/"),
        ("qr.example.test", "https://qr.example.test/path"),
        ("qr.example.test", "https://qr.example.test?query=1"),
        ("qr.example.test", "https://qr.example.test#fragment"),
        ("qr.example.test", "//qr.example.test"),
        ("qr.example.test", " https://qr.example.test"),
        ("qr.example.test", "https://QR.example.test"),
        ("qr.example.test", "https://qr.example.test."),
        ("replace-me.invalid", "https://replace-me.invalid"),
        ("example.com", "https://example.com"),
        ("localhost", "https://localhost"),
        ("127.0.0.1", "https://127.0.0.1"),
        ("another.example.test", "https://qr.example.test"),
    ],
)
def test_production_origin_rejects_every_noncanonical_form(
    domain: str,
    url: str,
) -> None:
    with pytest.raises(ValidationError):
        production_settings(public_domain=domain, public_base_url=url)


@pytest.mark.parametrize("trusted_proxy", ["*", "172.30.0.0/24", "172.31.0.10", ""])
def test_production_rejects_broad_or_wrong_proxy_trust(trusted_proxy: str) -> None:
    with pytest.raises(ValidationError):
        production_settings(forwarded_allow_ips=trusted_proxy)


@pytest.mark.parametrize("bind_host", ["0.0.0.0", "172.31.0.20", "127.0.0.1", ""])
def test_production_rejects_non_frontend_bind(bind_host: str) -> None:
    with pytest.raises(ValidationError):
        production_settings(app_bind_host=bind_host)


@pytest.mark.parametrize("mode", [None, "", "development", "Production"])
def test_compose_validator_rejects_missing_or_invalid_production_mode(
    mode: object,
) -> None:
    environment = {
        "DEPLOYMENT_MODE": mode,
        "APP_BIND_HOST": PRODUCTION_APP_IP,
        "FORWARDED_ALLOW_IPS": PRODUCTION_PROXY_IP,
    }

    with pytest.raises(ValueError, match="production mode must be hard-coded"):
        validate_app_boundary_environment(environment)


@pytest.mark.parametrize("bind_host", [None, "", "0.0.0.0", "172.31.0.20"])
def test_compose_validator_rejects_missing_or_invalid_app_bind(bind_host: object) -> None:
    environment = {
        "DEPLOYMENT_MODE": "production",
        "APP_BIND_HOST": bind_host,
        "FORWARDED_ALLOW_IPS": PRODUCTION_PROXY_IP,
    }

    with pytest.raises(ValueError, match="app bind address changed"):
        validate_app_boundary_environment(environment)


@pytest.mark.parametrize(
    "image",
    [
        None,
        "",
        "postgres:latest",
        "postgres:16",
        f"postgres:latest@sha256:{'0' * 64}",
        f"postgres@sha256:{'0' * 64}",
    ],
)
def test_compose_validator_rejects_mutable_or_malformed_image(image: object) -> None:
    with pytest.raises(ValueError):
        validate_image_reference("db", image)


def test_nginx_security_constants_are_explicit_and_token_safe() -> None:
    main = NGINX_MAIN.read_text(encoding="utf-8")
    site = NGINX_SITE.read_text(encoding="utf-8")

    for expected in (
        "client_header_timeout 10s;",
        "client_body_timeout 15s;",
        "client_body_buffer_size 128k;",
        "send_timeout 60s;",
        "keepalive_timeout 30s;",
        "proxy_connect_timeout 5s;",
        "proxy_send_timeout 60s;",
        "proxy_read_timeout 60s;",
        "proxy_buffering off;",
        "proxy_request_buffering on;",
        "proxy_max_temp_file_size 0;",
        "proxy_cache off;",
        "rate=60r/m",
        "rate=30r/m",
    ):
        assert expected in main

    assert "$request_uri" not in main
    assert "$request " not in main
    assert "$http_referer" not in main
    assert "error_log /var/cache/nginx/error.log crit;" in main

    for expected in (
        'Strict-Transport-Security "max-age=31536000; includeSubDomains" always;',
        "server_tokens off;",
        "limit_req zone=public_scan burst=120 nodelay;",
        "limit_req zone=admin_login burst=60 nodelay;",
        'add_header Retry-After "1" always;',
        'add_header Retry-After "2" always;',
        "client_max_body_size 51300k;",
        "client_max_body_size 10340k;",
        "proxy_set_header X-Forwarded-For $remote_addr;",
        'proxy_set_header X-Real-IP "";',
        'proxy_set_header Forwarded "";',
        "proxy_set_header X-Forwarded-Proto https;",
        "location = /health/ready",
        "ssl_reject_handshake on;",
        "proxy_cache off;",
    ):
        assert expected in f"{main}\n{site}"


def test_acme_is_the_only_static_location() -> None:
    site = NGINX_SITE.read_text(encoding="utf-8")

    assert "root " not in site
    assert site.count("alias ") == 1
    assert "alias /var/www/acme/.well-known/acme-challenge/$acme_token;" in site
    assert "try_files " not in site
    assert "disable_symlinks on from=/var/www/acme;" in site
    assert "location ^~ /.well-known/acme-challenge/" in site


def test_production_preflight_is_network_isolated_and_repeatable() -> None:
    wrapper = (ROOT / "scripts/production/prod-compose.sh").read_text(encoding="utf-8")
    compose = (ROOT / "compose.prod.yaml").read_text(encoding="utf-8")

    for expected in (
        "config --format json",
        'python3 "$repository_root/scripts/production/validate_compose.py"',
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges:true",
        '--env-file "$environment_file"',
        "--env DEPLOYMENT_MODE=production",
        "--env APP_BIND_HOST=172.30.0.20",
        "--env FORWARDED_ALLOW_IPS=172.30.0.10",
    ):
        assert expected in wrapper
    assert "compose run" not in wrapper
    assert wrapper.index("config --format json") < wrapper.index("docker run --rm")
    assert "DEPLOYMENT_MODE: production" in compose
    assert "APP_BIND_HOST: 172.30.0.20" in compose
    assert "FORWARDED_ALLOW_IPS: 172.30.0.10" in compose
    assert "${DEPLOYMENT_MODE" not in compose
    assert "${APP_BIND_HOST" not in compose
    assert "${FORWARDED_ALLOW_IPS" not in compose


def test_production_wrapper_bootstraps_an_empty_certificate_volume() -> None:
    wrapper = (ROOT / "scripts/production/prod-compose.sh").read_text(encoding="utf-8")
    bootstrap = (ROOT / "scripts/production/bootstrap-certificate.sh").read_text(encoding="utf-8")

    assert "ensure_bootstrap_certificate" in wrapper
    assert "test -s /tmp/active/fullchain.pem" in wrapper
    assert "test -s /tmp/active/privkey.pem" in wrapper
    assert "PRODUCTION_CERTIFICATE_BOOTSTRAP=1" in wrapper
    assert 'PRODUCTION_CERTIFICATE_BOOTSTRAP=1 "$compose" up --detach certbot' in bootstrap


def test_proxy_and_rotation_enforce_private_log_modes() -> None:
    compose = (ROOT / "compose.prod.yaml").read_text(encoding="utf-8")
    rotation = (ROOT / "scripts/production/rotate-logs.sh").read_text(encoding="utf-8")

    assert "chmod 0600 /var/cache/nginx/access.log /var/cache/nginx/error.log" in compose
    assert "umask 077" in rotation


def test_database_image_removes_unused_root_entrypoint_helper() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "AS database-runtime" in dockerfile
    assert "RUN unlink /usr/local/bin/gosu" in dockerfile


def test_application_image_removes_bytecode_build_cache() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "find /opt/venv /app -type d -name __pycache__ -prune -exec rm -rf {} +" in dockerfile


def test_proxy_and_certbot_have_locked_patched_build_targets() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "AS proxy-runtime" in dockerfile
    assert "RUN apk upgrade --no-cache" in dockerfile
    assert "AS certbot-runtime" in dockerfile
    assert "deploy/production/certbot/requirements.txt" in dockerfile
    assert "apk add --no-cache openssl=3.5.7-r0" in dockerfile
    certbot_requirements = (ROOT / "deploy/production/certbot/requirements.txt").read_text(
        encoding="utf-8"
    )
    assert "certbot==5.1.0" in certbot_requirements
    assert all(
        line.startswith("#") or "==" in line for line in certbot_requirements.splitlines() if line
    )
