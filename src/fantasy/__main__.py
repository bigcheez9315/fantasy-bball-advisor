import sys

from .cli import main

# Windows consoles default to cp1252, which can't print names like "Şengün".
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.exit(main())
