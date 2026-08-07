"""Tests for the local-safe process entry point."""

import io

import pytest

from product_pdf_qr import __main__
from product_pdf_qr import cli as admin_cli
from product_pdf_qr.config import get_settings
from product_pdf_qr.errors import AppError


def test_run_uses_centralized_loopback_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(_app: object, **kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://app_rw:synthetic@127.0.0.1:5432/test",
    )
    monkeypatch.delenv("APP_BIND_HOST", raising=False)
    monkeypatch.delenv("APP_PORT", raising=False)
    monkeypatch.setattr("product_pdf_qr.__main__.uvicorn.run", fake_run)
    get_settings.cache_clear()
    try:
        __main__.run()
    finally:
        get_settings.cache_clear()

    assert calls == [
        {
            "host": "127.0.0.1",
            "port": 8000,
            "proxy_headers": True,
            "forwarded_allow_ips": "127.0.0.1",
            "access_log": False,
        }
    ]


def test_admin_cli_exposes_no_password_value_argument() -> None:
    parser = admin_cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "create-admin",
                "--username",
                "owner",
                "--password",
                "MustNeverBeAccepted",
            ]
        )

    help_text = parser.format_help()
    assert "--password PASSWORD" not in help_text


def test_admin_cli_reads_password_from_stdin_without_echo(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "TemporaryPassword-123"
    captured_passwords: list[str] = []

    async def execute(_args: object, password: str) -> int:
        captured_passwords.append(password)
        return 0

    monkeypatch.setattr(admin_cli, "_execute", execute)
    monkeypatch.setattr("sys.stdin", io.StringIO(secret + "\n"))

    result = admin_cli.main(["create-admin", "--username", "owner", "--password-stdin"])

    output = capsys.readouterr()
    assert result == 0
    assert captured_passwords == [secret]
    assert secret not in output.out
    assert secret not in output.err


def test_admin_cli_interactive_confirmation_must_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(["FirstPassword-123", "DifferentPassword-456"])
    monkeypatch.setattr(
        "product_pdf_qr.cli.getpass.getpass",
        lambda _prompt: next(answers),
    )

    with pytest.raises(AppError) as captured:
        admin_cli.read_password(password_stdin=False)

    assert captured.value.code == "password_confirmation_mismatch"
