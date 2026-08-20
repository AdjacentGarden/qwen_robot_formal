#!/usr/bin/env python3
import os
import uvicorn

uvicorn.run(
    "app:app",
    host=os.environ.get("BIND_HOST", "0.0.0.0"),
    port=int(os.environ.get("BIND_PORT", "8765")),
    workers=1,
    access_log=False,
)
