"""Run the web dashboard + API. Run: python scripts/serve.py  then open http://127.0.0.1:8000

Uses ws="none" so it does not depend on the optional `websockets` package (the app has no websocket
endpoints). Equivalent CLI: `uvicorn resume_matcher.api.app:app --ws none --reload`.
"""
from __future__ import annotations

import logging
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def main() -> None:
    import uvicorn

    from resume_matcher.api.app import create_app

    # Leveled, timestamped logs so the app's own warnings (sweep failures, rejected uploads, autoload
    # errors) are visible alongside uvicorn's — controllable via RM_LOG_LEVEL.
    logging.basicConfig(
        level=os.environ.get("RM_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    host = os.environ.get("RM_HOST", "127.0.0.1")
    port = int(os.environ.get("RM_PORT", "8000"))
    print(f"Serving Resume Matcher on http://{host}:{port}  (backend: {os.environ.get('RM_INFERENCE_BACKEND', 'mock')})")
    uvicorn.run(create_app(), host=host, port=port, ws="none", log_level="info")


if __name__ == "__main__":
    main()
