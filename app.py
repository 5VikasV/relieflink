"""Vercel's FastAPI entrypoint; the local server uses the same application."""

from backend.app import app

__all__ = ["app"]
