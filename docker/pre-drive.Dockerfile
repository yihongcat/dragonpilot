FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/openpilot-venv \
    UV_CACHE_DIR=/opt/uv-cache \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    SCONS_CACHE=/opt/scons-cache

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      ca-certificates \
      curl \
      git \
      git-lfs \
      libcurl4-openssl-dev \
      locales \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /uvx /bin/

RUN git lfs install --system \
    && git config --global core.autocrlf false \
    && git config --global core.fileMode false \
    && git config --global --add safe.directory /workspace

WORKDIR /workspace

CMD ["bash", "scripts/pre_drive_check.sh", "--help"]
