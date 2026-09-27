# Blave Agent on k0s

Self-hosted Linux runtime of [Blave-TW/blave-agent](https://github.com/Blave-TW/blave-agent),
built from this checkout, without the Electron desktop shell or the Blave cloud control plane.

## What runs

One pod in namespace `blave-agent`:

| Piece | Role |
|---|---|
| `telegram_bridge.py` | Chat entry point; each message spawns one `agent_turn.py` (Claude Agent SDK) |
| `supercronic -inotify` | Runs the entries the agent writes with `crontab` (shimmed to `/opt/blave-agent/data/crontab`) |
| reconciler | **Not auto-started.** The agent starts it under tmux when you ask to start trading (upstream behaviour after a restart) |

Persistent data on PVC `blave-data` (`rustfs-nfs`, mounted at `/opt/blave-agent/data`):
`workspace/` (strategies, `state/`, `state/audit.jsonl`, `.env`), `config/telegram.json`,
`state/model_prefs.json`, `crontab`, `home/` (Claude CLI state).

## Models

- Default: Claude subscription via `CLAUDE_CODE_OAUTH_TOKEN`, model `claude-sonnet-5`.
- In chat, ask the agent to switch model; `litellm/<name>` ids (e.g. `litellm/qwen3.8`) go to
  `http://litellm.ai.svc.cluster.local:4000` with `BLAVE_LLM_API_KEY`. Local Qwen will be
  much weaker than Claude at this workload (AGENTS.md alone is ~12k tokens).

Patches applied at build time: `patch_runtime.py` (fails the build if upstream changes).

## Deploy

Interactive (prompts for sudo and each secret at hidden prompts, validates them, applies):
`bash deploy/k0s/setup.sh`. Or by hand:

```bash
# 1. build from the repo root and import into k0s containerd
docker build -f deploy/k0s/Dockerfile -t local/blave-agent:v1 .
docker save local/blave-agent:v1 | sudo k0s ctr --address /run/k0s/containerd.sock --namespace k8s.io images import -

# 2. secrets (typed by you; never commit them)
kubectl create namespace blave-agent
kubectl -n blave-agent create secret generic blave-agent-secrets \
  --from-literal=CLAUDE_CODE_OAUTH_TOKEN='<claude setup-token output>' \
  --from-literal=TELEGRAM_BOT_TOKEN='<BotFather token>' \
  --from-literal=TELEGRAM_ALLOWED_CHAT_ID='<your chat id>' \
  --from-literal=BLAVE_LLM_API_KEY='<litellm key, optional>'

# 3. apply
kubectl apply -f deploy/k0s/k8s/blave-agent.yaml
kubectl -n blave-agent logs -f deploy/blave-agent
```

Telegram troubleshooting (`poll error` in the logs):
- `401` / `404`: bad token. It must be the full `<digits>:<secret>` line from BotFather.
- `409 Conflict`: something else polls the same bot (Blave desktop/web pairing, a Claude
  Telegram plugin, an old script). Telegram allows one poller per token. Revoke the token in
  BotFather and put the new one only in the Secret.
- `TELEGRAM_ALLOWED_CHAT_ID` is *your* id (@userinfobot), not the bot's (the digits before `:`).

## Trading safety

- Starts with **paper trading only** — no exchange keys needed.
- Exchange keys, when you decide to go live, go in `workspace/.env` on the PVC
  (read + trade permissions only; never withdrawal). Add them yourself, e.g.
  `kubectl -n blave-agent exec -it deploy/blave-agent -- sh -c 'cat >> .env'`.
- Kill switch: `kubectl -n blave-agent exec deploy/blave-agent -- touch state/HALT`.
- Audit log: `workspace/state/audit.jsonl`.

## Yuanta (元大) broker

The image includes the .NET 8 runtime, `pythonnet` and Yuanta's SPARK API SDK at
`/opt/yuanta-sdk` (sha256-pinned; `--build-arg YUANTA_SDK=0` leaves it out, ~0.5 GB).
Order library: `lib/order_yuanta.py`; onboarding: `references/yuanta-broker.md`.
Put the `.pfx` under the PVC workspace (e.g. `credentials/yuanta.pfx`) and the `yuanta_*`
keys in `workspace/.env`, then `python3 lib/yuanta_probe.py` inside the pod. Without
`YUANTA_LIVE=true` it talks to Yuanta's UAT, which only accepts a whitelisted fixed IP.

## Data

Crypto K-lines come from Binance public endpoints (`BLAVE_KLINE_SOURCE=binance`).
Taiwan-stock public data and Blave-only series (indicators, options, calendars) need a
Blave account key in `workspace/.env`; without one those blocks are skipped.

## Not included (needs Blave's private control plane)

Web workspace (`web_bridge.py`), account / portfolio / report uploaders, scheduled
agent-narrated reports, automatic runtime updates.

## Updating from upstream

```bash
git fetch upstream && git merge upstream/main   # patch_runtime.py fails the build if a patch no longer applies
docker build -f deploy/k0s/Dockerfile -t local/blave-agent:v1 .
docker save local/blave-agent:v1 | sudo k0s ctr --address /run/k0s/containerd.sock --namespace k8s.io images import -
kubectl -n blave-agent rollout restart deploy/blave-agent
```

The runtime (`current/`) is replaced by the new image; the workspace on the PVC is not
overwritten — use `manager/update_workspace.py` or re-seed deliberately.
