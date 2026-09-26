import socket

s = socket.socket()
s.settimeout(3)
try:
    s.connect(("localhost", 5432))
    print("PG_PORT_OPEN=True")
except OSError as e:
    print(f"PG_PORT_OPEN=False ({e})")
finally:
    s.close()

import asyncpg, psycopg  # noqa
print("DRIVERS=ok")

import os

print("AGNES_API_KEY", bool(os.environ.get("AGNES_API_KEY", "").strip()))
print("AGNES_BASE_URL", os.environ.get("AGNES_BASE_URL"))
