# syntax=docker/dockerfile:1.7
# Guazza — multi-stage
# Stage 1: build del wheel (hatchling via PEP 517 `python -m build`)
# Stage 2: runtime con Python 3.13, nginx e frontend statico
#
# Tag immagine allineati a pyproject.toml (es. v0.9.0).
# Push trigger: workflow CI su push tag v*.*.*  (vedi .github/workflows/ci.yml).

FROM python:3.13-slim AS builder

WORKDIR /build

# `build` è il frontend PEP 517 standard sopra hatchling (specificato in pyproject.toml)
RUN pip install --no-cache-dir build

# Copia i manifest prima di src: cambiano meno spesso, meglio per la cache dei layer
COPY pyproject.toml ./
COPY src ./src

# Wheel in /wheels
RUN python -m build --wheel --outdir /wheels


FROM python:3.13-slim AS runtime

# nginx per il pod web (serve statici embedded + JSON dal PVC)
RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx \
    && rm -rf /var/lib/apt/lists/*

# Utente non-root UID 1000, allineato al pattern Houston (runAsUser: 1000)
# Il fsGroup del pod si occupa del fsGroupChangePolicy su /var/lib/guazza
RUN groupadd -g 1000 guazza \
    && useradd -u 1000 -g 1000 -m -s /bin/bash guazza

# Installa il wheel
COPY --from=builder /wheels/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl \
    && rm /tmp/*.whl

# Path applicazione (DB_PATH, OUTPUT_DIR, CONFIG_DIR montati dal pod)
WORKDIR /var/lib/guazza
RUN mkdir -p /var/lib/guazza/output /var/lib/guazza/logs \
    && chown -R guazza:guazza /var/lib/guazza

# Frontend statico embedded nell'immagine (esclude frontend/data/ symlink locale)
COPY --chown=guazza:guazza \
    frontend/index.html \
    frontend/app.js \
    frontend/style.css \
    /usr/share/nginx/html/

# Configurazione nginx per k8s (porta 8080, /data/ sul PVC, /health probe)
COPY deploy/nginx-k8s.conf /etc/nginx/conf.d/default.conf

# nginx va avviato con path scrivibili compatibili con readOnlyRootFilesystem:
#   - pid in /tmp
#   - log su /dev/stdout e /dev/stderr (vedi nginx-k8s.conf)
#   - body/proxy temp su /var/lib/nginx (scrivibile da guazza)
RUN sed -i 's|^pid .*|pid /tmp/nginx.pid;|' /etc/nginx/nginx.conf \
    && chown -R guazza:guazza /var/lib/nginx /var/log/nginx /tmp

USER guazza

EXPOSE 8080

# Default per il pod web. I CronJob k8s override con `command: ["guazza-X"]`.
CMD ["nginx", "-g", "daemon off;"]
