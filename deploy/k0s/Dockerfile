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

# Yuanta (元大) SPARK API for lib/order_yuanta.py: .NET 8 (ASP.NET Core) runtime + pythonnet
# + the vendor's linux-x64 Python component. Not in the repo (vendor binary) — fetched and
# pinned by sha256 here; a new Yuanta release fails the build until the hash is updated.
# Build with --build-arg YUANTA_SDK=0 to leave it out.
ARG YUANTA_SDK=1
ARG YUANTA_SDK_URL=https://ys.yuanta.com.tw/quartet/api/YuantaSparkAPI_linux-x64_Python.zip
ARG YUANTA_SDK_SHA256=0a5fb3f3ddce4a4409d9f01468fbe7402a37146c19307f4d6bc3abaad525df61
ENV DOTNET_ROOT=/usr/share/dotnet \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    YUANTA_SDK_DIR=/opt/yuanta-sdk
RUN if [ "$YUANTA_SDK" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends libicu72 unzip \
      && rm -rf /var/lib/apt/lists/* \
      && curl -fsSL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh \
      && bash /tmp/dotnet-install.sh --channel 8.0 --runtime aspnetcore --install-dir "$DOTNET_ROOT" \
      && rm /tmp/dotnet-install.sh \
      && /opt/blave-agent/venv/bin/pip install --no-cache-dir pythonnet \
      && curl -fsSL -o /tmp/yuanta.zip "$YUANTA_SDK_URL" \
      && echo "$YUANTA_SDK_SHA256  /tmp/yuanta.zip" | sha256sum -c - \
      && unzip -q /tmp/yuanta.zip -d /tmp/yuanta \
      && mv /tmp/yuanta/YuantaSparkAPI_linux-x64_Python "$YUANTA_SDK_DIR" \
      && rm -rf /tmp/yuanta /tmp/yuanta.zip \
      && test -f "$YUANTA_SDK_DIR/YuantaSparkAPI.dll"; \
    fi

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
# Not workspace/: that symlink dangles until entrypoint creates data/workspace on the PVC
WORKDIR /opt/blave-agent
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/usr/local/bin/entrypoint.sh"]
