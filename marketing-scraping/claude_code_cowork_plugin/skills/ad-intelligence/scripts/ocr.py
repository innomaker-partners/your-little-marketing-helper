"""
OCR utility — reads ad copy off a rendered ad image using a cheap-model subagent.

Google Ads Transparency renders text ads as images, so the scraper cannot
extract their copy as a string. This module transcribes the text back off
the downloaded image by invoking the Claude Code CLI (`claude -p --model haiku`)
in headless mode. The subagent reads the image with its own Read tool and
returns the transcription. No API key required beyond the Claude Code CLI
authentication that is already in place.

Design rule (FROZEN — do not change without a deliberate decision):
  This is an OCR utility, not an analysis step. Always use the cheapest
  available model (`haiku`) with minimal, transcription-only prompting.
  Upgrading the model or enriching the prompt inverts the cost-discipline
  intent for marginal gain.

Subprocess rules, the same as every other
`claude` call in this plugin (pain report.py, search report.py, quote_tool.py):
  - `--strict-mcp-config --mcp-config '{"mcpServers":{}}'` starves the
    subprocess of the operator's MCP servers. Without them a `claude --print`
    boots the whole MCP stack: browser servers, blank windows, minute stalls.
  - No timeout. A model call that is slow is normal, not a hang (report.py's timeout was
  removed under that rule). The old
    30-second limit cut off slow transcriptions and the run then said there
    were no images to transcribe.

Stdlib only — no pip install needed.
"""

import shutil
import subprocess
from pathlib import Path

# ---------------------------------------------------------------------------
# Frozen prompt constant — do NOT change (see module docstring)
# ---------------------------------------------------------------------------
OCR_PROMPT = (
    "Transcribe all text in this advertisement image. "
    "Output only the transcribed text, nothing else."
)

# Default model — cheapest vision-capable model the Claude Code CLI accepts.
# Overridable via the `model` argument (cfg `vision_ocr_model`).
# Do NOT upgrade — the frozen design rule prohibits it.
_DEFAULT_MODEL = "haiku"

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ocr_ad_image(image_path, model=""):
    """
    Transcribe text from an ad image using a cheap-model subagent.

    Invokes `claude --print --model haiku` (Claude Code headless mode). The
    subagent reads the image at `image_path` using its own Read tool and
    returns the transcription. No API key required.

    Args:
        image_path: Path to the image file (str or Path).
        model:      Model alias. Empty string means "haiku" (cheapest).
                    Accepts any alias the `claude` CLI understands.
                    Do NOT pass a bigger model — see frozen design rule.

    Returns:
        Transcribed text (str), or "" on any failure (graceful degradation).
    """
    return transcribe(image_path, model)[0]


def transcribe(image_path, model=""):
    """Same call as ocr_ad_image, returning (text, outcome) so the caller can say
    which case it was:
      "ok"      text came back;
      "empty"   the call finished and returned no text (an image with no words);
      "failed"  the call did not finish (non-zero exit, could not start);
      "missing" the `claude` command-line tool was not found.
    WHY: an image that was read and held no text is not a transcription that
    "did not finish", and the closing text must not say it was."""
    if not shutil.which("claude"):
        print("[ocr] `claude` CLI not found in PATH, so OCR is skipped.")
        return "", "missing"

    abs_path = str(Path(image_path).resolve())
    resolved_model = model.strip() if model.strip() else _DEFAULT_MODEL

    # Subagent prompt: name the absolute image path so the Read tool can load
    # it, then apply the frozen transcription instruction.
    prompt = f"Read the image at {abs_path}. {OCR_PROMPT}"

    # Pass the prompt via stdin so --allowedTools (variadic) does not consume it.
    cmd = [
        "claude",
        "--print",
        "--output-format", "text",
        # Stop the subprocess from booting the parent's MCP stack.
        "--strict-mcp-config",                # ignore ~/.claude.json MCP servers
        "--mcp-config", '{"mcpServers":{}}',  # ...and supply an empty set
        "--model", resolved_model,
        "--allowedTools", "Read",
        "--add-dir", str(Path(abs_path).parent),
    ]

    # No timeout: a model call gets none (see the module docstring).
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print("[ocr] `claude` CLI not found, so OCR is skipped.")
        return "", "missing"
    except Exception as exc:
        print(f"[ocr] Unexpected error for {image_path}: {exc}")
        return "", "failed"

    if result.returncode != 0:
        stderr_snippet = (result.stderr or "").strip()[:200]
        print(
            f"[ocr] Subagent returned non-zero ({result.returncode}) for {image_path}"
            + (f": {stderr_snippet}" if stderr_snippet else "")
        )
        return "", "failed"

    text = (result.stdout or "").strip()
    if not text:
        print(f"[ocr] Subagent returned empty output for {image_path}")
        return "", "empty"

    return text, "ok"
