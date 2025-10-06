#!/usr/bin/env python3
import datetime as dt
import platform
print("healthcheck: OK")
print("time_utc:", dt.datetime.utcnow().isoformat(timespec="seconds") + "Z")
print("python:", platform.python_version())
