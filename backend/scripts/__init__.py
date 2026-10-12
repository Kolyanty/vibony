"""Dev tooling for the backend (not shipped in the `vibony_backend` package).

`backend/` is on `sys.path` via the editable poetry install, so `import
scripts.<module>` resolves here. Nothing under `scripts/` is imported by the
running app or collected by the default test run.
"""
