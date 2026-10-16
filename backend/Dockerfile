# Runtime image for the API, the worker and migrations (same image, different commands).
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

RUN useradd --system --uid 10001 --no-create-home sfci \
 && mkdir -p /var/lib/sfci/artifacts \
 && chown sfci /var/lib/sfci/artifacts
# Content-addressed artifact store (SFCI_ARTIFACT_ROOT); mount a volume here.
VOLUME /var/lib/sfci/artifacts
USER sfci

EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

# Test image: runtime plus test dependencies and the test suite.
FROM runtime AS test
USER root
COPY requirements-dev.txt ./
RUN pip install -r requirements-dev.txt
COPY pyproject.toml ./
COPY tests ./tests
USER sfci
CMD ["pytest", "-q"]
