# {{service_name}}: multi-stage, non-root image (python:3.12-slim; builds natively on arm64/Graviton and amd64).
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install -r /tmp/requirements.txt
COPY gunicorn.conf.py /srv/
COPY app/ /srv/app/
RUN /opt/venv/bin/python -m compileall -q /srv/app

FROM python:3.12-slim
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT={{port}}
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid 10001 --no-create-home app
COPY --from=build /opt/venv /opt/venv
COPY --from=build /srv /srv
WORKDIR /srv
# Runs as non-root; only /tmp must be writable (an emptyDir in Kubernetes, so readOnlyRootFilesystem works).
USER 10001:10001
EXPOSE {{port}}
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app.wsgi:app"]
