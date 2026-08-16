"""The weekly pull schedule (section 11e). Times are user-local (America/Chicago)
by design -- stored as intended local time + zone so DST does not shift them.

Run under cron / systemd timer / APScheduler in deploy; in dev, pulls are
triggered from the UI. Listed here so the schedule is code, not folklore.
"""
PULL_SCHEDULE = [
    ("Wed", "12:00"), ("Thu", "12:00"), ("Fri", "12:00"),
    ("Sat", "12:00"), ("Sat", "17:00"), ("Sat", "21:00"),
    ("Sun", "06:00"), ("Sun", "08:00"),
    ("Sun", "10:30"),   # post-inactives -- highest-value pull; alert on failure
    ("Sun", "11:15"),
]
TIMEZONE = "America/Chicago"
