"""Controlled administrator provisioning commands."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Sequence

from product_pdf_qr.config import get_settings
from product_pdf_qr.database import Database
from product_pdf_qr.domains.auth import PasswordManager, create_admin, reset_admin_password
from product_pdf_qr.errors import AppError


def build_parser() -> argparse.ArgumentParser:
    """Build commands that deliberately expose no password argument."""

    parser = argparse.ArgumentParser(prog="python -m product_pdf_qr.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("create-admin", "创建唯一管理员"),
        ("reset-password", "重置管理员密码并强制首次改密"),
    ):
        subparser = subparsers.add_parser(command, help=help_text)
        subparser.add_argument("--username", required=True)
        subparser.add_argument(
            "--password-stdin",
            action="store_true",
            help="从标准输入读取一行密码; 不在命令行参数中传密码",
        )
    return parser


def read_password(*, password_stdin: bool) -> str:
    """Read a secret from an interactive terminal or one stdin line."""

    if password_stdin:
        password = sys.stdin.readline()
        # The empty string is an input sentinel, not a hard-coded credential.
        if password == "":  # nosec B105
            raise AppError("password_input_empty", "标准输入中没有密码。", 422)
        return password.removesuffix("\n").removesuffix("\r")

    password = getpass.getpass("密码: ")
    confirmation = getpass.getpass("再次输入密码: ")
    if password != confirmation:
        raise AppError("password_confirmation_mismatch", "两次输入的密码不一致。", 422)
    return password


async def _execute(args: argparse.Namespace, password: str) -> int:
    settings = get_settings()
    database = Database(settings)
    password_manager = PasswordManager()
    await database.open()
    try:
        if args.command == "create-admin":
            admin_id = await create_admin(
                database,
                password_manager,
                raw_username=args.username,
                password=password,
            )
            print(f"管理员已创建 (id={admin_id}, username={args.username.strip()})。")
            return 0
        admin_id = await reset_admin_password(
            database,
            password_manager,
            raw_username=args.username,
            password=password,
        )
        print(f"管理员密码已重置 (id={admin_id}, username={args.username.strip()})。")
        return 0
    finally:
        await database.close()


def main(argv: Sequence[str] | None = None) -> int:
    """Run one provisioning operation without echoing or logging secret material."""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        password = read_password(password_stdin=bool(args.password_stdin))
        return asyncio.run(_execute(args, password))
    except AppError as error:
        print(error.message, file=sys.stderr)
        return 1
    except Exception:
        print("命令执行失败。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
