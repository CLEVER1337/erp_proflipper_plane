# ERP deploy: root Dockerfile so GitLab CI (`docker build -t ... .`) builds the
# Plane API from the repo root, like the other ERP services. Build context is the
# repo root; all COPY paths are under apps/api/. Api-only container (no worker/beat).

# Base image is parameterised so restricted networks can pull it from a mirror
# registry, e.g. --build-arg BASE_IMAGE=dockerhub.timeweb.cloud/library/python:3.12.10-alpine
ARG BASE_IMAGE=python:3.12.10-alpine
FROM ${BASE_IMAGE}

# set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV INSTANCE_CHANGELOG_URL=https://sites.plane.so/pages/691ef037bcfe416a902e48cb55f59891/

# Optional mirrors for restricted networks (RF). Defaults are empty => upstream,
# so the public image is unchanged. Pass e.g.:
#   --build-arg ALPINE_MIRROR=https://mirror.yandex.ru/mirrors/alpine
#   --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG ALPINE_MIRROR=
ARG PIP_INDEX_URL=
RUN if [ -n "$ALPINE_MIRROR" ]; then \
      sed -i "s|https://dl-cdn.alpinelinux.org/alpine|$ALPINE_MIRROR|g" /etc/apk/repositories; \
    fi

# Update system packages for security
RUN apk update && apk upgrade

WORKDIR /code

RUN apk add --no-cache --upgrade \
    "libpq" \
    "libxslt" \
    "xmlsec" \
    "ca-certificates" \
    "openssl"

COPY apps/api/requirements.txt ./
COPY apps/api/requirements ./requirements
RUN apk add --no-cache libffi-dev
RUN apk add --no-cache --virtual .build-deps \
    "bash~=5.2" \
    "g++" \
    "gcc" \
    "cargo" \
    "git" \
    "make" \
    "postgresql-dev" \
    "libc-dev" \
    "linux-headers" \
    && \
    pip install ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} -r requirements.txt --compile --no-cache-dir \
    && \
    apk del .build-deps \
    && \
    rm -rf /var/cache/apk/*


# Add in Django deps and generate Django's static files
COPY apps/api/manage.py manage.py
COPY apps/api/plane plane/
COPY apps/api/templates templates/
COPY apps/api/package.json package.json
# Non-secret ERP config (secrets come from env). Loaded by settings/common.py.
COPY apps/api/erp_config.json erp_config.json

RUN apk --no-cache add "bash~=5.2"
COPY apps/api/bin ./bin/

RUN mkdir -p /code/plane/logs
RUN chmod +x ./bin/*
RUN chmod -R 777 /code

# Expose container port and run entry point script
EXPOSE 8000

# Api-only container that migrates itself, runs erp_bootstrap, and skips create_bucket.
CMD ["./bin/docker-entrypoint-api-erp.sh"]
