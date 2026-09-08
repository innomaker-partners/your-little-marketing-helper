"""
The follower classifier: a Claude (Haiku) subagent over the `claude` CLI.

This is the one judgment step of the pipeline, and it is deliberately a SUBSCRIPTION
subagent, not an API call: it shells out to the `claude` CLI in --print mode. That
matches the house rule (LLM steps run as a claude-CLI subagent, never an API) and
means the only credential the whole tool needs is the PhantomBuster token; the Claude
side rides the user's subscription.

Two hard-won constraints are baked in:

  1. Starve the MCP stack. A bare `claude` subprocess boots the operator's entire MCP
     stack (browser servers -> blank windows, minute-long stalls). The flags
     --strict-mcp-config --mcp-config '{"mcpServers":{}}' start it with NO servers.
     `_smoke_test_no_browser` in the tests asserts no browser process appears.

  2. No timeout. An LLM/subagent call is non-deterministic; slowness is normal, not a
     hang. Deterministic HTTP calls get timeouts (see pb_client); this does not.

The output contract is owned by code (pipeline.extract_classification + coerce_label),
not by the prompt: whatever the model says -- a bare label, JSON, prose-wrapped -- is
parsed tolerantly and snapped to the taxonomy, so a user editing the categories can
never break the pipeline.
"""

from __future__ import annotations

import json
import subprocess
from typing import Callable

import pipeline as P

# The four fields the classifier sees per follower -- identical to the n8n original,
# so a follower is judged on employer/company signal, not on URLs or social links.
CLASSIFIER_FIELDS = ["companyName", "companyIndustry",
                     "linkedinCompanyDescription", "linkedinJobTitle"]

DEFAULT_MODEL = "haiku"

# Flags that start claude with an empty MCP config -- see constraint 1 above.
_STARVE_MCP = ["--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": {}})]


def run_claude(prompt: str, model: str = DEFAULT_MODEL) -> str:
    """
    Run one `claude --print` call and return its stdout. No timeout (constraint 2).
    Raises RuntimeError on a non-zero exit so a broken CLI surfaces loudly rather
    than silently producing blank labels.
    """
    cmd = ["claude", "--print", "--model", model, *_STARVE_MCP]
    proc = subprocess.run(cmd, input=prompt, text=True, capture_output=True)  # noqa: S603
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude CLI exited {proc.returncode}: {proc.stderr.strip()[:500]}"
        )
    return proc.stdout.strip()


def build_system_prompt(taxonomy: dict) -> str:
    """
    Build a strict single-label classifier prompt from a user-editable taxonomy.
    The shape (one label out, catch-all on ambiguity) is fixed; the labels, the
    fallback, and any extra instructions are the user's.
    """
    labels = taxonomy.get("labels", [])
    fallback = taxonomy.get("fallback", labels[-1] if labels else "Other")
    extra = taxonomy.get("instructions", "")
    label_list = " OR ".join(f'"{l}"' for l in labels)
    return (
        "You are a strict classifier. Based ONLY on the company/employer and role in "
        "the input, output exactly one of: " + label_list + ".\n"
        "Never output anything else: no punctuation, no quotes, no explanation, no "
        "extra lines. If the data is insufficient or ambiguous, output "
        f'"{fallback}".\n'
        + (extra + "\n" if extra else "")
        + "Output exactly one label and nothing else."
    )


def build_user_message(record: dict) -> str:
    """The four-field input block, in the n8n original's format."""
    return (
        f"companyName: {record.get('companyName', '')}\n\n"
        f"linkedinCompanyIndustry: {record.get('companyIndustry', '')}\n\n"
        f"linkedinCompanyDescription: {record.get('linkedinCompanyDescription', '')}\n\n"
        f"linkedinJobTitle: {record.get('linkedinJobTitle', '')}"
    )


def classify_one(record: dict, system_prompt: str, taxonomy: dict,
                 model: str = DEFAULT_MODEL,
                 runner: Callable[[str, str], str] = run_claude) -> str:
    """
    Classify one follower. `runner` is injectable so tests can substitute a fake
    model without shelling out. Returns a label already snapped to the taxonomy.
    """
    prompt = system_prompt + "\n\nInput:\n" + build_user_message(record) + "\n\nOutput:"
    raw = runner(prompt, model)
    label = P.extract_classification(raw)
    return P.coerce_label(label, taxonomy.get("labels"),
                          taxonomy.get("fallback", "Other"))


def classify_followers(records: list[dict], taxonomy: dict,
                       model: str = DEFAULT_MODEL,
                       runner: Callable[[str, str], str] = run_claude,
                       system_prompt: str | None = None) -> list[str]:
    """
    Classify a list of followers, one subagent call each (mirrors the n8n per-item
    design). Sequential and untimed -- slowness is expected on a subscription path.
    Returns labels aligned to `records`.
    """
    system_prompt = system_prompt or build_system_prompt(taxonomy)
    return [classify_one(r, system_prompt, taxonomy, model, runner) for r in records]
