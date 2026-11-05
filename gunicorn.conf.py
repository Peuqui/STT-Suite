"""gunicorn's settings. Where it listens comes from the environment, the same variables server.py reads
for its access rules (auth.py): STT_HOST beyond loopback only with a token."""
import os

bind = f"{os.environ['STT_HOST']}:{os.environ['STT_PORT']}"
# A long recording takes as long as it takes.
timeout = 0
# One worker with two threads: a single model per device, and the health check answers during a transcription.
workers = 1
threads = 2
