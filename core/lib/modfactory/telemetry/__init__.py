"""Local execution telemetry.

Observational only: ``job.json`` stays authoritative. Collection uses the
standard library, stays on this machine, and fails open unless
``[telemetry].fail_open`` is false.
"""
