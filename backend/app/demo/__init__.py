"""Demo mode (docs/demo.md): recorded CI inputs replayed through the real
backend, in its own database, with demo-only signers.

Nothing in this package decides a build's status, a failure class or a gate
result. It only produces inputs (webhooks, run reports, artifact zips,
gates.yml) and hands them to the same endpoints and services production uses.
"""
