"""Local-safe command-line entry point."""

import uvicorn

from product_pdf_qr.config import get_settings
from product_pdf_qr.main import app


def run() -> None:
    """Run the application using centralized bind settings."""

    settings = get_settings()
    uvicorn.run(
        app,
        host=settings.app_bind_host,
        port=settings.app_port,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        access_log=False,
    )


if __name__ == "__main__":
    run()
