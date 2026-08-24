"""Single source of truth for the backend's semantic version.

Bump ``__version__`` when you cut a release and add a matching entry to the
release notes (ai-portal-frontend/public/docs/release-notes.md). This value is
surfaced in the OpenAPI schema (FastAPI ``version``) and the ``/api/version``
endpoint.

The build/commit identifier is separate and updates automatically on every
deploy — it is read from the ``APP_GIT_COMMIT`` env var, which the CI deploy
job injects with the pushed commit SHA (see .gitlab-ci.yml / Dockerfile).
"""

__version__ = "1.5.0"
