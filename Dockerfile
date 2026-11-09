# Backend image. The SAME image runs the API and the worker, only the start command differs.
# Put this file in claim-ops/ (next to pyproject.toml).  Build:  docker build -t opd-backend .
# Your pyproject.toml requires Python >=3.14, so the base image is 3.14.
FROM python:3.14-slim

# UV_PYTHON*: use the Python that is already in this image (3.14). Without this, uv downloaded the newest
# Python, which can be too new for some libraries.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON=/usr/local/bin/python \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/srv/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /srv

# 1) libraries first, so Docker re-uses this layer when only your code changes
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 2) your code
COPY app ./app

# never run as root inside the container
# only these folders need to be writable (/logs: main.py works out its log folder one level too high inside the container). (A recursive chown of /srv copied the multi-GB .venv
# into a second layer and made the first build take 40 extra minutes.)
RUN useradd --create-home appuser && mkdir -p /srv/uploads /srv/logs /logs && chown appuser:appuser /srv/uploads /srv/logs /logs
USER appuser

EXPOSE 8000 8001
CMD ["python", "-m", "app.main"]
# the worker uses:  python -m app.worker.claims_worker