FROM python:3.12-slim

WORKDIR /app

# Core deps run the full app on the deterministic Mock backend (no model / GPU needed).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt fastapi "uvicorn>=0.29"

COPY resume_matcher ./resume_matcher
COPY scripts ./scripts

# Demo defaults: Mock backend, auto-load synthetic data, bind all interfaces.
# RM_ADMIN_PASSWORD MUST be supplied at runtime (the app runs OPEN if it is unset).
ENV RM_INFERENCE_BACKEND=mock \
    RM_AUTOLOAD=1 \
    RM_HOST=0.0.0.0 \
    RM_PORT=8000

EXPOSE 8000

# serve.py runs uvicorn with ws="none" (the app has no websockets; avoids a uvicorn/websockets clash).
# Single process on purpose: app state is in-memory, so one worker keeps it consistent.
CMD ["python", "scripts/serve.py"]
