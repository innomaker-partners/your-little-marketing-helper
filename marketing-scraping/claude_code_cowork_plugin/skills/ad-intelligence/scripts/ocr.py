"""
OCR utility — reads ad copy off a rendered ad image using a cheap-model subagent.

Google Ads Transparency renders text ads as images, so the scraper cannot
extract their copy as a string. This module transcribes the text back off
the downloaded image by invoking the Claude Code CLI (`claude -p --model haiku`)
in headless mode. The subagent reads the image with its own Read tool and
returns the transcription. No API key required beyond the Claude Code CLI
authentication that is already in place.

Design rule (FROZEN — do not change without owner approval):
  This is an OCR utility, not an analysis step. Always use the cheapest
  available model (`haiku`) with minimal, transcription-only prompting.
  Upgrading the model or enriching the prompt inverts the cost-discipline
  intent for marginal gain.

Stdlib only — no pip install needed.
"""

import shutil
import subprocess
from pathlib import Path

# ---------------------------------------------------------------------------
# Frozen prompt constant — do NOT change (owner rule, see module docstring)
# ---------------------------------------------------------------------------
OCR_PROMPT = (
    "Transcribe all text in this advertisement image. "
    "Output only the transcribed text, nothing else."
)

# Default model — cheapest vision-capable model the Claude Code CLI accepts.
# Overridable via the `model` argument (cfg `vision_ocr_model`).
# Do NOT upgrade — the frozen design rule prohibits it.
_DEFAULT_MODEL = "haiku"

# Subagent timeout in seconds. Ad copy is short; 30 s is generous.
_TIMEOUT_SECONDS = 30


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
    if not shutil.which("claude"):
        print("[ocr] `claude` CLI not found in PATH — OCR skipped.")
        return ""

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
        "--model", resolved_model,
        "--allowedTools", "Read",
        "--add-dir", str(Path(abs_path).parent),
    ]

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        print("[ocr] `claude` CLI not found — OCR skipped.")
        return ""
    except subprocess.TimeoutExpired:
        print(f"[ocr] Subagent timed out after {_TIMEOUT_SECONDS}s for {image_path}")
        return ""
    except Exception as exc:
        print(f"[ocr] Unexpected error for {image_path}: {exc}")
        return ""

    if result.returncode != 0:
        stderr_snippet = (result.stderr or "").strip()[:200]
        print(
            f"[ocr] Subagent returned non-zero ({result.returncode}) for {image_path}"
            + (f": {stderr_snippet}" if stderr_snippet else "")
        )
        return ""

    text = (result.stdout or "").strip()
    if not text:
        print(f"[ocr] Subagent returned empty output for {image_path}")
        return ""

    return text
