FROM python:3.13-slim

# Don't buffer output (live progress in `docker logs`), no .pyc files
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/app:${PATH}"

WORKDIR /app

# Debian security updates that are newer than the base image
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# pip is only needed at build time. Removing it also removes the packages it
# bundles (urllib3, msgpack, setuptools), which are often behind on fixes.
RUN pip install -r requirements.txt \
 && pip uninstall -y pip

COPY --chmod=755 *.py ./

# Unprivileged user. The scripts read config.ini and write cache/ relative
# to the working directory, so /data is the place to mount both.
RUN useradd --create-home --uid 1000 immich \
 && mkdir /data \
 && chown immich:immich /data
USER 1000:1000
WORKDIR /data
VOLUME ["/data"]

# Batch tools, not a service: nothing to health-check
HEALTHCHECK NONE

# Any script can be passed as command, e.g. `list_albums.py`
CMD ["add_image_description.py"]
