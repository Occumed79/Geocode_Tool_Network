from __future__ import annotations

import os
import sys

from gunicorn.app.wsgiapp import run


def main() -> None:
    port = os.getenv("PORT", "8080")
    sys.argv = [
        "gunicorn",
        "main:app",
        "--bind",
        f"0.0.0.0:{port}",
        "--workers",
        "1",
        "--timeout",
        "300",
    ]
    run()


if __name__ == "__main__":
    main()
