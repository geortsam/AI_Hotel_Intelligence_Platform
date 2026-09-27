"""Operator commands, run as ``python -m app.jobs.<name>`` from the API image.

Each command wraps one job function that already lives in the service layer, uses the
application's own settings and database infrastructure, prints counts only, and exits non-zero
when it fails. None schedules itself: when, and how often, a command runs is a deployment
decision. None is reachable over HTTP.
"""
