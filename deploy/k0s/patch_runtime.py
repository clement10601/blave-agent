"""Build-time patches for running the Blave Agent runtime without the Blave proxy.

Upstream assumes model ids routed by api.blave.org (deepseek/…, anthropic/…).
Here a turn runs either on the Claude subscription (CLAUDE_CODE_OAUTH_TOKEN,
bare Claude model ids) or, for ids prefixed "litellm/", through the in-cluster
LiteLLM (BLAVE_LLM_BASE_URL / BLAVE_LLM_API_KEY). Every replacement must match
exactly once, so an upstream change fails the image build instead of silently
shipping an unpatched runtime.
"""
import sys
from pathlib import Path

root = Path(sys.argv[1])


def patch(name, old, new):
    path = root / name
    src = path.read_text()
    count = src.count(old)
    if count != 1:
        sys.exit(f"patch_runtime: {name}: expected 1 match, found {count}:\n{old}")
    path.write_text(src.replace(old, new))
    print(f"patch_runtime: {name} ok")


# 1. Default model comes from the environment, not the Blave proxy catalogue;
#    proxy-style ids map onto something this machine can actually run.
patch(
    "model_prefs.py",
    'DEFAULT_MODEL = "deepseek/deepseek-v4-pro"\n\nVISION_MODEL = "anthropic/claude-sonnet-5"',
    'DEFAULT_MODEL = os.environ.get("BLAVE_DEFAULT_MODEL", "claude-sonnet-5")\n\n'
    'VISION_MODEL = os.environ.get("BLAVE_VISION_MODEL", "claude-sonnet-5")',
)
patch(
    "model_prefs.py",
    "        return VISION_MODEL\n    return model\n",
    "        return VISION_MODEL\n"
    "    # k8s: no Blave proxy here — anthropic/<id> is the bare Claude id, and\n"
    "    # deepseek/<id> (proxy-only) falls back to the machine default.\n"
    '    if model.startswith("anthropic/"):\n'
    '        return model.split("/", 1)[1]\n'
    '    if model.startswith("deepseek/"):\n'
    "        return DEFAULT_MODEL\n"
    "    return model\n",
)

# 2. litellm/<id> turns go to the in-cluster LiteLLM instead of the subscription.
patch(
    "agent_turn.py",
    '        turn_env.pop("ANTHROPIC_BASE_URL", None)\n'
    '        turn_env.pop("ANTHROPIC_API_KEY", None)\n',
    '        turn_env.pop("ANTHROPIC_BASE_URL", None)\n'
    '        turn_env.pop("ANTHROPIC_API_KEY", None)\n'
    '        if model and model.startswith("litellm/") and os.environ.get("BLAVE_LLM_BASE_URL"):\n'
    '            model = model.split("/", 1)[1]\n'
    '            turn_env["ANTHROPIC_BASE_URL"] = os.environ["BLAVE_LLM_BASE_URL"]\n'
    '            turn_env["ANTHROPIC_AUTH_TOKEN"] = os.environ.get("BLAVE_LLM_API_KEY", "")\n'
    '            turn_env["CLAUDE_CODE_OAUTH_TOKEN"] = ""\n',
)

# 3. The model-switch rule points at the Blave proxy's /v1/models; list local options instead.
patch(
    "agent_turn.py",
    '        f\'curl -s {PROXY_BASE_URL}/v1/models -H "x-api-key: $ANTHROPIC_API_KEY"\\n\'\n'
    '        "```\\n"\n'
    '        "`$ANTHROPIC_API_KEY` 已經在你的環境變數裡（本 runtime 的 proxy token），\\n"\n'
    '        "不需要另外要金鑰，直接呼叫就有正確、即時的清單跟計價。\\n\\n"\n',
    '        "echo \\"$BLAVE_MODEL_CATALOG\\"\\n"\n'
    '        "```\\n"\n'
    '        "（本機自架：Claude 模型走訂閱，`litellm/` 開頭的走叢集內 LiteLLM，"\n'
    '        "沒有計價資料。）\\n\\n"\n',
)
