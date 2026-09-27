# Blave Agent — self-hosted Linux runtime (no Electron shell, no Blave cloud control plane)
# Build from the repo root:  docker build -f deploy/k0s/Dockerfile -t local/blave-agent:v1 .
FROM python:3.12-slim-bookworm

ARG SUPERCRONIC_VERSION=v0.2.49

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Taipei \
    BLAVE_AGENT_BASE=/opt/blave-agent \
    BLAVE_AGENT_HOME=/opt/blave-agent \
    BLAVE_AGENT_WORKSPACE=/opt/blave-agent/workspace \
    BLAVE_AGENT_PYTHON=/opt/blave-agent/venv/bin/python3 \
    HOME=/opt/blave-agent/data/home \
    PATH=/opt/blave-agent/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl tini tmux procps tzdata jq \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL -o /usr/local/bin/supercronic \
      "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64" \
 && chmod 0755 /usr/local/bin/supercronic

# Same package set the desktop app installs into ~/Blave/venv (README › where things live).
RUN python -m venv /opt/blave-agent/venv \
 && /opt/blave-agent/venv/bin/pip install --no-cache-dir \
      claude-agent-sdk cryptography pandas numpy matplotlib pyarrow requests python-dotenv scipy

# This checkout becomes the seed for the workspace PVC on first start.
COPY . /opt/blave-agent/src

# Runtime payload (what publish.py ships to cloud machines) + local patches.
RUN mkdir -p /opt/blave-agent/current \
 && cp /opt/blave-agent/src/runtime/*.py /opt/blave-agent/src/runtime/VERSION /opt/blave-agent/current/ \
 && python /opt/blave-agent/src/deploy/k0s/patch_runtime.py /opt/blave-agent/current \
 && cp /opt/blave-agent/src/deploy/k0s/sync_notify_compat.py /opt/blave-agent/sync_notify_compat.py \
 && install -m 0755 /opt/blave-agent/src/deploy/k0s/entrypoint.sh /usr/local/bin/entrypoint.sh \
 && install -m 0755 /opt/blave-agent/src/deploy/k0s/crontab-shim.sh /usr/local/bin/crontab \
 && CLI="$(find /opt/blave-agent/venv -path '*claude_agent_sdk/_bundled/claude' -type f | head -1)" \
 && test -n "$CLI" && ln -s "$CLI" /usr/local/bin/claude

# Everything mutable lives on the PVC mounted at /opt/blave-agent/data.
RUN useradd --uid 10001 --user-group --no-create-home --home-dir /opt/blave-agent/data/home blaveagent \
 && mkdir -p /opt/blave-agent/data \
 && for d in config state workspace credentials; do ln -s data/$d /opt/blave-agent/$d; done \
 && ln -s data/openclaw.json /opt/blave-agent/openclaw.json \
 && chown blaveagent:blaveagent /opt/blave-agent/data

USER 10001:10001
WORKDIR /opt/blave-agent/workspace
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/usr/local/bin/entrypoint.sh"]
