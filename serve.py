"""Launch URL Matcher Studio web app."""

from __future__ import annotations

import os

import uvicorn


def _host() -> str:
    explicit = os.getenv("HOST")
    if explicit:
        return explicit
    # Railway and other PaaS set PORT; bind all interfaces in that case.
    return "0.0.0.0" if os.getenv("PORT") else "127.0.0.1"


def _port() -> int:
    return int(os.getenv("PORT", "8787"))


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=_host(),
        port=_port(),
        reload=False,
        proxy_headers=os.getenv("PORT") is not None,
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "*"),
    )
