"""
Loopback protection nonce — generated once at process startup.
Shared between server (inbound check) and pipeline (outbound inject).
"""

import secrets

NONCE = secrets.token_hex(16)
NONCE_HEADER = "x-mind-span-nonce"
