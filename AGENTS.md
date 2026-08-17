# Repository Guidelines

## Project Structure & Module Organization

Application code lives in `src/things_manager/`. The central `app.py` module defines the Flask application, SQLAlchemy models, routes, authorization helpers, CSV workflows, and database initialization. Jinja pages are in `src/things_manager/templates/`; there is currently no separate static-assets directory. Root-level `app.py` is the local-development compatibility entry point, while `wsgi.py` exposes the production application to Gunicorn. Keep tests under `tests/`; `instance/` contains ignored runtime database state and must not be treated as source.

## Build, Test, and Development Commands

- `python -m venv .venv && source .venv/bin/activate` creates and activates a local environment.
- `pip install -r requirements.txt` installs the pinned Flask, SQLAlchemy, and Gunicorn dependencies.
- `python app.py` runs the development server at `http://127.0.0.1:5000` by default.
- `python -m unittest discover -v` runs the complete test suite.
- `python -m unittest tests.test_app_smoke.AppSmokeTest.test_health_and_wsgi_entrypoint` runs one test.
- `docker build -t things-system:v1.1.1 .` builds the production image; its default command serves `wsgi:app` with Gunicorn.

## Coding Style & Naming Conventions

Use four-space indentation and PEP 8 conventions for Python. Name functions, variables, and model fields with `snake_case`, classes and SQLAlchemy models with `PascalCase`, and constants with `UPPER_SNAKE_CASE`. Route paths should remain lowercase. No formatter or linter is configured, so match surrounding code carefully. In Jinja templates, preserve the local HTML indentation and use `camelCase` for inline JavaScript functions.

## Testing Guidelines

Tests use standard-library `unittest` and Flask’s test client. Name files `test_*.py`, classes `*Test`, and methods `test_*`. Add coverage for successful behavior, validation failures, authentication, and role boundaries. Tests must use in-memory SQLite and must never alter `instance/storage.db`; no numeric coverage threshold is currently enforced.

## Commit & Pull Request Guidelines

History is small and mixed, but recent commits use `feat:` and `fix:` prefixes. Prefer focused, imperative Conventional Commit subjects such as `fix: validate borrow quantities`, using an ASCII colon. Pull requests should explain the behavior change, link relevant issues, list verification commands, and include screenshots for template or UI changes.

## Security & Configuration

Use `.env.example` as the configuration reference. Never commit `.env`, secrets, or files under `instance/`; production requires a strong `SECRET_KEY`. Importing the application initializes and seeds the database unless `AUTO_INIT_DB=0`, so set configuration before importing it in scripts and tests.
