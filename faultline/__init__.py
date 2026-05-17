__version__ = "0.11.0"

# Apply Anthropic auto-streaming patch before any stage constructs a
# client. Without this, every messages.create with max_tokens >= 8K
# raises "Streaming is required" on Haiku and intermittently on Sonnet
# — stages catch the exception and silently degrade. See
# faultline/_streaming_autoenable.py for the full rationale.
from faultline import _streaming_autoenable  # noqa: F401,E402
