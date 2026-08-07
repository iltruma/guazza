FROM python:3.13-slim AS runtime

# uv dal binario ufficiale (niente pip, niente wheel intermedio)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# nginx + wget + libgomp (OpenMP per lightgbm) + utente non-root
RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx wget libgomp1 \
    && rm /etc/nginx/sites-enabled/default \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 guazza && useradd -u 1000 -g 1000 guazza

# Codice (pyproject prima di src per cache)
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
# Config dell'app versionata con l'immagine (unica source of truth: repo guazza)
COPY config ./config
ENV CONFIG_DIR=/app/config

# Install (uv gestisce build isolation automaticamente)
RUN uv pip install --system --no-cache .

# Statici + nginx-k8s
COPY frontend/index.html frontend/affidabilita.html frontend/app.js frontend/style.css /usr/share/nginx/html/
COPY deploy/nginx-k8s.conf /etc/nginx/conf.d/default.conf

# Permessi e pid compatibile con readOnlyRootFS
WORKDIR /var/lib/guazza
RUN chown -R guazza:guazza /var/lib/guazza /var/lib/nginx /var/log/nginx \
    && sed -i 's|^pid .*|pid /tmp/nginx.pid;|' /etc/nginx/nginx.conf

USER guazza
EXPOSE 8080
HEALTHCHECK CMD wget -q -O- http://localhost:8080/health || exit 1
CMD ["nginx", "-g", "daemon off;"]
