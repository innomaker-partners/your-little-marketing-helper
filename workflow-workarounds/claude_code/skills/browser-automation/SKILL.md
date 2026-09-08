---
name: browser-automation
description: Develop deterministic browser automation scripts for any web workflow using Claude-in-Chrome MCP tools
---

# Browser Script Developer

**Your deliverable is a script, not data.** When a user asks you to automate a browser
workflow, your job is to develop a deterministic, re-runnable script and package it wherever
the user wants. Data produced during development is test evidence that the script works. It
is never the deliverable by itself.

There are three layers. This skill lives at the top one:

- **L0 - the run.** Extracted data, created drafts. Meaningless by themselves. A run's
  output is evidence that the script works, not the artifact the user ends up with.
- **L1 - the automation.** A deterministic, re-runnable script that performs the workflow.
  This is what the user keeps.
- **L2 - this skill (the product).** A teaching skill that makes your agent capable of
  developing L1 for any browser workflow the user needs, and packaging it wherever they want:
  their own skill directory, a working folder, or a standalone script. LinkedIn Analytics and
  Outlook Web Drafts are worked examples for learning the methodology. They are not the
  product's feature list.

A script artifact is the acceptance criterion. The script either exists and re-runs
deterministically, or it does not. Data visible on screen is not a pass.

**This plugin's own files are addressed from `${CLAUDE_PLUGIN_ROOT}`.** This skill's reference
material - `REFERENCE.md` and the baseline selectors in `scripts/browser/dom_patterns.json` -
ships inside the plugin. `${CLAUDE_PLUGIN_ROOT}` is the absolute path Claude Code sets to this
plugin's install location; prepend it when you read one of them (e.g.
`${CLAUDE_PLUGIN_ROOT}/scripts/browser/dom_patterns.json`). The plugin directory is not the
user's working directory, so a bare relative path will not resolve.

### Doing the task by hand is the failure this skill exists to prevent — and it hides

Everything above you have read before, in one form or another, and it has not been enough: the
strongest pull in this work is to *do the task* instead of *build the thing that does the task*, and
it wins quietly. The answer is right there on the page, so the model reads it and types it, the screen
looks correct, and a run ends with the numbers in place and no script behind them. It does not feel
like failure. It is the failure.

The line is exact. Probing the live surface by hand to **learn how to write the artifact** is the
whole build loop and is expected. **Producing the deliverable's data by hand is not.** A value you
obtained with your own eyes and fingers — something you read, an earlier probe, a number you typed in
— is *evidence*, never *output*. The deliverable's data is only ever what your artifact emits when you
run it. If you are holding a figure that did not come out of a run of your artifact, it does not go in
the deliverable.

**So you are not done until the artifact produced the result cold — and the gate for "done" is your
report, not your judgment.** Asking yourself "did I do this right?" does not work: a model that has
drifted into hand-doing skips the question, or answers it wrongly, and a self-assessment and a real
check read identically on the page. So the gate is not a question you answer; it is two things your
report must **contain**. A completion report's default verdict is **INCOMPLETE**. You earn **DONE**
only by exhibiting both of these — and a report missing either is an INCOMPLETE report, not a DONE
report with a gap:

1. **The artifact's path** — the file on disk that *is* the deliverable, one you can point to and
   invoke. A `javascript_tool` snippet you ran once, a function you defined inside a tool call, a
   value you read with your eyes or typed with your fingers — these are **probes**, not artifacts.
   They live in your call history, not on disk; there is nothing to hand the user and nothing to
   re-run. If this field carries no path, you built nothing — go build it. (This is the exact hole
   the word "function" used to leave open: a function you executed in the page is not a stored file.)

2. **The cold-run transcript** — the exact invocation of that file and the raw output it emitted,
   pasted verbatim, with the wanted result appearing *inside that output*. "The proving run" below
   fixes the transcript's precise shape: the artifact's own `ARTIFACT_ID`-stamped return object, plus
   the two integrity checks that prove what ran equals what is stored.

A field is filled **only** by pasting what a command or a tool printed — never by narrating, never
from memory, never from an earlier probe. A sentence asserting you ran the artifact does not fill
field 2: "I executed the file and it worked" is a sentence a model under load writes without doing
the work behind it, which is exactly how a hand-done task gets reported as done. The paste is the one
piece of evidence a hand-doer structurally cannot manufacture — they have no stored file to invoke and
no from-scratch output to show — which is why the report is built around producing it rather than
around promising it.

If the cold run did not produce the wanted result, the verdict stays INCOMPLETE: fix the artifact and
run it cold again, with no hand-step in between, and repeat until a from-scratch run produces the
result. A segment that is genuinely unscriptable — no DOM to drive *and* no app-native automation
door (see "When the surface has no reachable DOM") — is reported as an honest STOP with a handoff,
never faked into the cold run; every scriptable segment still passes this gate.

## Pre-flight

Run ALL checks before starting any development session. Do not skip any.

### 1. Claude-in-Chrome connection

Call `tabs_context_mcp` to verify the extension is connected. If it fails:

> "The Claude-in-Chrome extension is not connected. Install it from the Chrome Web Store,
> then add it as an MCP server: run `claude mcp add claude-in-chrome` in your terminal and
> restart Claude Code."

### 2. Chrome profile

Check which Chrome profile is active. If multiple profiles exist, ask:

> "I see multiple Chrome profiles. Which one has the accounts you want to automate?"

Only proceed with the confirmed profile.

### 3. Site permissions

Verify the Claude-in-Chrome extension has site-level permissions for the target domain.
If permissions are missing:

> "The Claude-in-Chrome extension needs permission to access [domain]. Open the extension
> settings in Chrome and add [domain] to the allowed sites."

For Outlook: both `outlook.office.com` and `outlook.cloud.microsoft` need permission.
Microsoft 365 redirects the first to the second silently. Check which domain you actually
land on after navigation, not the one you navigated to.

### 4. Authentication pre-condition

Confirm the user is manually logged into the target account before dispatching any
automation. An agent cannot click login form submit buttons or approve re-authentication
prompts without explicit per-session user consent. Do not navigate and discover the session
is broken mid-run.

### 5. Keep the tab active in its window

Before starting, tell the user:

> "Keep the tab I am working in as the active tab of its browser window, and do not minimize
> that window. You are free to switch to other apps or windows and keep working while the
> automation runs. I will tell you if I ever need this tab brought back to the front."

Chrome does not paint a tab that is not the active tab of its window, so a screenshot of a
background tab comes back blank, and Chrome throttles that tab's in-page timers. Window focus
does not matter for capture: an unfocused window, behind other windows or behind another
application, is captured normally, and in-page JavaScript (`javascript_tool`) runs against it
normally. Do not treat `document.hidden` or `visibilityState` as a signal
that the tab can or cannot be driven; it reports page visibility, not capturability, and
reading it as a capability check produces false stops. The signal that a tab is capturable is a
screenshot that returns real pixels.

### 6. Real input needs input focus — and this asymmetry is a trap

Capture and in-page JavaScript reach any tab that is the active tab of its window, regardless of
window focus (above). **Real pointer and keyboard input from the `computer` tool are different:
they land only on the tab that currently holds input focus — the active tab of the frontmost
window.** A tab that screenshots with real pixels can still silently swallow every `computer`
keystroke and click if it is a background tab, or if its window is not the frontmost one. The page
looks perfectly drivable and your input just vanishes; nothing changes and no error is raised.

When `computer` input is not registering, suspect input focus FIRST — is this the active tab of its
window, and is that window frontmost? — and do not conclude the control is unreachable until you have
confirmed input focus and retried. Unregistered input read as a wall is a classic false stop: a real
DOM control on a background tab, or a trusted click sent while the window is not frontmost, looks
"walled" when the only problem is that its tab was never made active. This is a different thing from a
surface that genuinely has no per-element DOM — a `<canvas>` or a cross-origin frame — which no amount
of focus can fix; that case is handled in "When the surface has no reachable DOM" (don't drive it
live, and look for the app's own automation before concluding STOP).

**The architecture this asymmetry dictates: do everything you can headless, and let focus be a brief,
late commit.** Because probes, reads, and every in-page `javascript_tool` step run without focus, the
whole development loop and every scriptable part of a run cost the person nothing — they keep working
in another window the entire time. Only a step that genuinely needs *trusted* input — a download's
real click, or committing through an app's own affordance when there is no other door — needs the
screen, and only for that moment. So structure the automation to make that irreducible foreground step
the *last* thing, fully prepared for in advance (data staged, target and keystrokes known), and take
the screen once, briefly, then hand it back. A run that holds the screen from discovery onward — like
driving a canvas live — is a design mistake, not a demand of the task; the same job splits into a long
headless build and a foreground commit measured in seconds.

**Two-surface workflows — read from one page, write to another tab — must be sequenced around this.**
Only one tab per window receives input, so you cannot hold both surfaces live at once. Read the
source surface to completion and store the values you need in your own variables; THEN activate the
destination tab so it becomes the active tab of its window, and confirm from a screenshot that it is
active before you type the first character. Activating the destination deactivates the source — that
is expected, which is why you read the source fully first. Re-confirm activation after any tab switch,
and keep the destination tab active for the whole write phase.

Two things this section does **not** license. First, it is not a reason to foreground a window
**pre-emptively** or "to be safe": foregrounding takes the screen away from whatever the person is
doing, so you do it only at the one irreducible commit, never in advance and never defensively. (When
that one commit does arrive, you raise your *own* window yourself — see "The brief foreground commit"
above — rather than handing the job to the user by default.) Second, if you find yourself
needing sustained frontmost focus to place keystroke after keystroke into a surface you cannot address
or read through the DOM, you are no longer doing browser automation — you are hand-operating an app
live. That is not a technique: stop driving it live and look for the app's own automation (its script
or macro engine), and STOP for real only if there is none (see "When the surface has no reachable
DOM").

## Substrate

This skill uses **Claude-in-Chrome MCP tools** exclusively. "Exclusively" is a hard rule, not a
default: when a control resists, the answer is a better browser technique or an honest STOP — never a
different tool that reaches the same data another way (an API, an MCP connector, an off-browser file
edit). See "The browser is the only surface — a blocked path is a STOP, never a tool-switch" under
Safety rules; that prohibition is load-bearing, not advisory.

**`javascript_tool`** evaluates in-page JavaScript and returns the result. One mechanic that
causes failures if missed: `javascript_tool` evaluates its code as a page-context
**expression**, not a function body. A bare `return` at the top level is a SyntaxError.
Wrap all multi-step logic in an IIFE:

```javascript
(async function() {
  const el = document.querySelector('button[aria-label^="New"]');
  if (!el) return JSON.stringify({ found: false });
  el.click();
  await new Promise(r => setTimeout(r, 1000));
  return JSON.stringify({ found: true, clicked: true });
})()
```

For simple single-expression probes, a plain expression suffices:

```javascript
JSON.stringify(document.documentElement.lang)
```

If `javascript_tool` returns `{}` from an async IIFE, the cause is an unawaited Promise;
see the browser-automation error reference in `${CLAUDE_PLUGIN_ROOT}/REFERENCE.md` for the working pattern and why the action
inside may still have executed.

**`computer` tool** is the documented fallback for controls that check `event.isTrusted`.
Some UI controls (certain file inputs, native dialog buttons) reject synthetic events and
require real pointer simulation. Use `computer` only when `javascript_tool` simulation has
demonstrably failed for a specific control. To click with `computer`: scroll the element
into view (`el.scrollIntoView()`), read its viewport position with
`el.getBoundingClientRect()`, compute the center coordinates (left + width/2, top +
height/2), and pass those coordinates to `computer` as a click action; then verify the
result by DOM readback exactly as you would after a synthetic event.

**Coordinates are in CSS pixels; the `computer` tool's space may not be.**
`getBoundingClientRect()` returns CSS pixels. On a HiDPI/Retina display, or when the OS or
browser is zoomed, the `computer` tool's coordinate space is scaled relative to CSS pixels, so a
click at the computed CSS centre lands off the target — consistently short of it by the scale
factor, so the same wrong coordinates miss the same way however many times you repeat them. The
readback catches it: if the target did not change state (the dialog is still open, the button did
not react), do not re-click the same point. Screenshot to see where the click landed, derive the
factor from the offset, apply it, click again, and read back. This factor is a per-run,
per-display observation — **do not bake it into the artifact as a constant.** A stored automation
runs on displays you have not seen and at zoom levels that change between runs; a hardcoded scale
is wrong the moment either differs. Keep the in-page script returning CSS coordinates and let this
click-and-verify loop, which you perform between the script's phases, absorb the scaling fresh
each run.

**`read_page`** returns a structured snapshot of the current page. Use it to probe page
structure before building selectors.

**`navigate`** changes the current URL. Always verify the actual landed URL after
navigation: redirects are silent and return no error. Navigating to `outlook.office.com`
redirects to `outlook.cloud.microsoft`; navigating to `linkedin.com/analytics/` redirects
to the feed when the URL is wrong. Check the body for error markers too.

## The script contract

Every automation you develop must have this shape. Each element exists because its absence
caused a documented live failure. Removing any element requires disclosure.

```javascript
await (async function() {   // `await` is REQUIRED, not decoration: javascript_tool runs in a
                            // top-level-await REPL that does NOT auto-await a trailing Promise. A
                            // bare `(async function(){...})()` returns the pending Promise, which
                            // serialises to `{}` — an artifact that looks broken on its proving run
                            // though the logic is fine. See "The proving run" (the `{}` signature).
// ============================================================
// [AUTOMATION NAME]
// What it does: [one sentence describing the automation]
// Prerequisites: [logged-in to X, tab open on Y, data file at Z]
// Parameters (injected before this IIFE in the javascript_tool call):
//   ITEMS      - array of objects; each must have a unique `key` field from source data
//   START_IDX  - resume from this index (0 for a fresh run)
//   BATCH_SIZE - max items to process in this call
// Returns (JSON string). Every return path carries `artifact`, a self-identifying stamp
// copied from ARTIFACT_ID below, so a report reader can tell the object came from executing
// THIS file and not from a probe, a screenshot, or filesystem metadata. A return object with
// no `artifact` field is not evidence the artifact ran.
//   Normal completion: { artifact: ARTIFACT_ID, completed: N, errors: [{index, key, error}], lastIndex: N }
//     On a clean run: completed matches items processed, errors is []
//   Pre-flight stop:  { artifact: ARTIFACT_ID, stopped: true, reason: 'selector_mismatch',
//                       missing: [keyNames], completedBeforeStop: N, lastIndexBeforeStop: N }
//     Fix the missing selectors in SEL, then resume from lastIndexBeforeStop.
//     Do not improvise a workaround: selector mismatch means the page changed.
// Known fallbacks: [list any technique that needed a fallback during development]
//
// RUN-TIME RULES (embedded here because this script runs without this plugin):
//   - Any SEL selector returning null at run time: STOP and report. Do not improvise.
//   - Partial mismatch (some selectors resolve, others do not): also STOP.
//   - CAPTCHA or challenge dialog: stop immediately, no retries, 30+ min rest,
//     user owns the clock.
//   - NEVER click the selectors marked DO_NOT_CLICK below.
// ============================================================

// --- artifact identity (self-identifying stamp for the proving run) ---
// Set at freeze time to this automation's name and freeze date, e.g. 'outlook-drafts YYYY-MM-DD'.
// It is echoed in every return object (see the return-shape contract above) so the proving
// run's output is traceable to THIS file and cannot be mimicked by filesystem metadata or a
// probe snippet. PLACEHOLDER: set before the proving run.
const ARTIFACT_ID = 'PLACEHOLDER: automation-name YYYY-MM-DD';

// --- selector table (empirical facts from the live page; never guessed patterns) ---
const SEL = {
  composeButton: 'button[aria-label^="New"]',       // PLACEHOLDER: replace with verified value from live page
  toField:       'div[contenteditable="true"][aria-label*="To"]',            // PLACEHOLDER: replace with verified value from live page
  subjectField:  'input[aria-label*="Subject"]',                             // PLACEHOLDER: replace with verified value from live page
  bodyField:     'div[contenteditable="true"][aria-label*="Message body"]',  // PLACEHOLDER: replace with verified value from live page
  // Forbidden selectors (DO NOT CLICK under any circumstances):
  // PLACEHOLDER: replace these with the multi-language selectors from
  // ${CLAUDE_PLUGIN_ROOT}/scripts/browser/dom_patterns.json (keys outlook_office.send_button_DO_NOT_CLICK and
  // outlook_office.discard_button_DO_NOT_CLICK for Outlook; derive from the live
  // aria-label probe for other platforms), then verify each against the live page.
  // Path (authoritative): dom_patterns.json lives at
  // ${CLAUDE_PLUGIN_ROOT}/scripts/browser/dom_patterns.json. Before concluding the file is
  // missing, search from the plugin root; if still not found, report the search performed,
  // not a conclusion of absence.
  SEND_DO_NOT_CLICK:    'button[aria-label*="Send"]',    // PLACEHOLDER: see comment above
  DISCARD_DO_NOT_CLICK: 'button[aria-label*="Discard"]', // PLACEHOLDER: see comment above
};

// --- timing constants (tune by observation during development; values below are starting points only) ---
const T = {
  afterClick:      1200,   // PLACEHOLDER: tune by observation; ms to wait for DOM to settle after a click
  fieldSettle:      500,   // PLACEHOLDER: tune by observation; ms after dispatching input/paste events
  autosaveTimeout: 8000,   // PLACEHOLDER: tune by observation; ms to poll for autosave confirmation
  pollInterval:     200,   // ms between waitForElement polls (rarely needs changing)
};

// --- standard helpers ---

async function waitForElement(selector, timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const el = document.querySelector(selector);
    if (el) return el;
    await new Promise(r => setTimeout(r, T.pollInterval));
  }
  throw new Error(`Timeout: selector not found in ${timeoutMs}ms: ${selector}`);
}

function simulateClick(el) {
  // Full pointer sequence for React-family event handlers.
  // A bare .click() often misses pointerdown/mousedown listeners.
  for (const type of ['pointerdown','mousedown','pointerup','mouseup','click']) {
    el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true }));
  }
}

function setNativeValue(inputEl, value) {
  // For <input> and <textarea> controlled by React.
  // Does NOT apply to div[contenteditable] elements: use pasteIntoContenteditable instead.
  const tag = inputEl.tagName;
  const proto = tag === 'INPUT'    ? HTMLInputElement.prototype
              : tag === 'TEXTAREA' ? HTMLTextAreaElement.prototype
              : null;
  if (!proto) throw new Error(`setNativeValue: not an input/textarea (got ${tag})`);
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(inputEl, value);
  inputEl.dispatchEvent(new Event('input', { bubbles: true }));
}

function pasteIntoContenteditable(el, htmlContent) {
  // For contenteditable editors and address wells.
  // Required because these are div elements; the value-setter prototype technique
  // does not apply to div elements.
  el.focus();
  const dt = new DataTransfer();
  dt.setData('text/html', htmlContent);
  dt.setData('text/plain', el.textContent);
  el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
}

// --- main automation function ---

async function run(items, startIdx, batchSize) {
  const result = { artifact: ARTIFACT_ID, completed: 0, errors: [], lastIndex: startIdx };

  // Coverage requirement: every non-forbidden SEL key must appear in exactly one of
  // PAGE_KEYS, SURFACE_KEYS, or ACTION_KEYS. An empty array is valid only when this script
  // genuinely has no selectors of that kind; disclose that in the script header.
  // ACTION_KEYS: selectors used to perform actions mid-run, verified at time of use rather
  // than in pre-flight, because they may exist only in certain page states; do not add
  // ACTION_KEYS entries to the stage-1 or stage-2 filter expressions.
  // Auditor check: Object.keys(SEL).filter(k => !k.includes('DO_NOT_CLICK'))
  //   must equal [...PAGE_KEYS, ...SURFACE_KEYS, ...ACTION_KEYS] (order-independent).
  // The auditor formula must never be modified per-script. A selector that fits no existing
  // class means the classification is wrong, not the formula.
  const PAGE_KEYS    = [/* PLACEHOLDER: page-level SEL key names, e.g. 'composeButton' */];
  const SURFACE_KEYS = [/* PLACEHOLDER: surface-level SEL key names, e.g. 'toField', 'subjectField', 'bodyField' */];
  const ACTION_KEYS  = [/* PLACEHOLDER: mid-run action SEL key names, e.g. 'firstMessage'; empty if none */];

  // Stage 1: page-level pre-flight (runs once, before any item is processed).
  // Checks selectors present on the initial page state, before any interaction.
  // Routing: selectors the script waits on or reads back after an action belong in
  // SURFACE_KEYS (stage 2); selectors the script clicks or acts on mid-run belong in
  // ACTION_KEYS (verified at time of use, not in pre-flight).
  // Both stages carry progress fields so resumption after a selector fix is possible
  // without re-running completed items.
  const missingPage = PAGE_KEYS.filter(k => !document.querySelector(SEL[k]));
  if (missingPage.length > 0) {
    return { artifact: ARTIFACT_ID, stopped: true, reason: 'selector_mismatch', missing: missingPage,
             completedBeforeStop: result.completed, lastIndexBeforeStop: result.lastIndex };
  }

  for (let i = startIdx; i < Math.min(startIdx + batchSize, items.length); i++) {
    const item = items[i];
    result.lastIndex = i;
    try {
      // [state-establishing action goes here, e.g. open compose surface]

      // Stage 2: surface-level pre-flight (runs once per item, after state is established).
      // Checks selectors that only exist after the action above.
      const missingSurface = SURFACE_KEYS.filter(k => !document.querySelector(SEL[k]));
      if (missingSurface.length > 0) {
        return { artifact: ARTIFACT_ID, stopped: true, reason: 'selector_mismatch', missing: missingSurface,
                 completedBeforeStop: result.completed, lastIndexBeforeStop: result.lastIndex };
      }

      // [remaining per-item steps]
      result.completed++;
    } catch (err) {
      result.errors.push({ index: i, key: item.key, error: err.message });
      break; // stop the batch; return for inspection before continuing
    }
  }

  return result;
}

return JSON.stringify(await run(ITEMS, START_IDX, BATCH_SIZE));
})()
```

Every value in the SEL table and every constant in T is a placeholder; replace each with a value verified from your live page, and for the forbidden entries use the multi-language selectors from `${CLAUDE_PLUGIN_ROOT}/scripts/browser/dom_patterns.json` rather than the English-only examples shown here.

**Data injection**: `ITEMS`, `START_IDX`, and `BATCH_SIZE` are injected at the top of the
string you pass to `javascript_tool`, before this IIFE. Build the data payload as a
separate step (e.g. a small Python script producing a JSON file). Logic and data are never
mixed in the same string.

**Why each element exists:**

- **SEL table**: class-name keyword selectors (`[class*="metric"]`) returned `[]` on the
  live LinkedIn analytics page because LinkedIn uses obfuscated hashed class names like
  `_04113bfd f9d54e6d`. Every selector in SEL must be verified from the live page during
  development.
- **`simulateClick`**: a bare `.click()` misses `pointerdown`/`mousedown` handlers that
  React-family components use to track interaction.
- **`setNativeValue`**: React controlled inputs ignore `.value =` assignments without the
  prototype setter. Note the constraint: this applies only to `<input>` and `<textarea>`,
  not to `div[contenteditable]` elements.
- **`pasteIntoContenteditable`**: address wells and rich-text editors are div elements.
  The value-setter technique cannot apply to a div. The Outlook To field is a
  `div[contenteditable="true"]`, and a paste event is the only correct technique.
- **Batch/resume signature**: any run over multiple items can fail partway through. The
  `startIdx` parameter and `errors` return allow re-entry without reprocessing completed
  items. Keys must come from source data, not positional indexes, because index positions
  shift after a failure.
- **Forbidden selectors in SEL**: the prohibition must travel with the script. An agent
  running this artifact without this plugin must see which controls are off-limits,
  written in the artifact itself.
- **Header-as-contract**: run-time rules are embedded in the header because the artifact
  runs later under agents that have never read this plugin.
- **Two-stage pre-flight**: selectors have state preconditions. Page-level selectors are
  present on the initial page state before any interaction; surface-level selectors appear
  only after a specific action establishes the required state (e.g. after a compose surface
  opens for each item). A single pre-flight over all selectors before item 0 will fail for
  any creation workflow whose compose surface opens inside the loop. Classify every SEL
  entry in the script header as `[page-level]`, `[surface: after <action>]`, or
  `[action: mid-run]` (the third label maps to ACTION_KEYS) and place it in the
  corresponding category. Coverage is required: the union of PAGE_KEYS, SURFACE_KEYS,
  and ACTION_KEYS must equal all non-forbidden SEL keys (auditor check:
  `Object.keys(SEL).filter(k => !k.includes('DO_NOT_CLICK'))`). ACTION_KEYS holds selectors
  used to perform actions mid-run, verified at time of use. An empty array is valid only when
  the script genuinely has no selectors of that kind, which must be stated in the script
  header. The auditor formula must never be modified per-script; a selector that fits no
  existing class means the classification is wrong, not the formula.

## The development loop

For each step of the target workflow, in this order.

### 1. Probe the live DOM

Run `read_page` to get the page structure. Then run the aria-label enumeration probe to see
what controls are actually present:

```javascript
(function() {
  const lang = document.documentElement.lang || 'unknown';
  const labeled = [];
  document.querySelectorAll('[aria-label]').forEach(el => {
    const label = el.getAttribute('aria-label');
    if (label && label.trim()) {
      labeled.push({ tag: el.tagName.toLowerCase(), role: el.getAttribute('role'), label: label.trim() });
    }
  });
  return JSON.stringify({ lang, labeledElements: labeled.slice(0, 80) });
})()
```

Run the framework probe:

```javascript
(function() {
  const hook = window.__REACT_DEVTOOLS_GLOBAL_HOOK__;
  let hasRenderer = false;
  try { hasRenderer = !!(hook && hook.renderers && (hook.renderers.size > 0 || Object.keys(hook.renderers).length > 0)); } catch(e) {}
  return JSON.stringify({
    react:   hasRenderer || !!document.querySelector('[data-reactroot],[data-reactid]'),
    angular: !!document.querySelector('[ng-version],[ng-app]'),
    vue:     !!(document.querySelector('[data-v-]') || window.__VUE__),
  });
})()
```

**Framework probe warning**: a result of all-false ("static") does not mean CSS class names
are stable or readable. Some platforms hide their framework hook and obfuscate class names
with content hashes that change on deployment. Treat a "static" result as "framework
undetected," not as "standard keyword selectors will work."

After every navigation: verify the actual landed URL and check the page body for error
markers before proceeding. Silent redirects are a documented failure mode.

Run the CAPTCHA probe after every page load. The selectors in the snippet below are illustrative, drawn from `${CLAUDE_PLUGIN_ROOT}/scripts/browser/dom_patterns.json` under `linkedin.captcha_indicators`. When running on a platform that has a `captcha_indicators` list in dom_patterns.json, load that list as the base and extend it with any additional indicators the live probe reveals; replace the array in the snippet with the combined list before running:

```javascript
(function() {
  const captchaSelectors = [
    // Load from dom_patterns.json for the current platform and extend with live probe findings.
    // The values below are the linkedin.captcha_indicators entries, shown here as illustration.
    '#captcha-challenge', '.challenge-dialog',
    '[data-test-id="challenge"]', 'iframe[src*="captcha"]'
  ];
  const matched = captchaSelectors.filter(s => { try { return !!document.querySelector(s); } catch(e) { return false; } });
  const body = document.body ? document.body.innerText.toLowerCase() : '';
  const keyword = ['verify you are human','security check','prove you are not a robot'].find(k => body.includes(k)) || null;
  const detected = matched.length > 0 || keyword !== null;
  return JSON.stringify({ captchaDetected: detected, matchedSelectors: matched, keywordMatch: keyword,
    action: detected ? 'STOP - follow CAPTCHA protocol in Safety Rules' : 'clear' });
})()
```

If `captchaDetected: true`: follow the CAPTCHA protocol in Safety Rules immediately.

### 2. Hypothesize a selector

From what the probe returned, form one selector hypothesis. Prefer, in order:

1. `aria-label` selectors for interactive controls: they survive framework re-renders and
   work in localized UIs. Use prefix matching (`[aria-label^="New"]`) rather than full
   strings when labels may vary by tenant. Microsoft ships different aria-label strings for
   the same control to different tenant configurations.
2. Role plus attribute combinations for structural regions.
3. Tag plus attribute anchored on stable content.

Do not use:
- Generic class-keyword selectors (`[class*="metric"]`) unless you have confirmed the live
  page uses semantic class names.
- Full aria-label strings for controls known to vary across tenants (use prefix matching).
- Positional selectors (`:nth-child`) unless nothing else is available.
- The generic `[role="textbox"][contenteditable="true"]` as a unique selector when a page
  has multiple contenteditable elements. Anchor the body field on its own aria-label; the
  To field on its own aria-label. A generic match can target the wrong element and paste
  message body content into the recipient well.

### 3. Test in isolation

One `javascript_tool` call, one selector or action. Do not test multiple things in one
call during the probe phase.

**During development, a selector returning null is normal iteration.** Probe again and
revise. This is different from run-time behavior. The distinction is architectural:

- **Build-time** (now, during development): selector failure means revise. Keep probing.
  This phase is exploratory by design.
- **Run-time** (after the script is frozen): selector failure means the page no longer
  matches the frozen script. STOP and report. Never improvise a workaround.

Do not conflate the two modes. The development loop expects and tolerates failures. The
frozen script does not.

**A blocked, empty, or inert result is a different obstacle than a null selector, and it needs a
different reflex.** A selector returning `null` you handle by revising the selector. But sometimes the
tool call itself does not do what you expected: `javascript_tool` returns `{}` or an error, a click
reports success yet nothing on the page changes, or a write seems to run and leaves the surface
untouched. Here the question is not "which selector" but "what is actually in the way," and the reflex
to resist is **naming a cause you have not observed** (a missing permission, a site grant, a timing
issue) and then shipping a fix for it. A guessed cause sends you repairing the wrong thing while the
real obstacle stands untouched.

Probe the obstacle in isolation first, with a constants-only snippet that asserts nothing about the
page's content, only about the substrate:

```javascript
(function() {
  // Constants-only obstacle probe: asserts nothing about page content, only about the substrate.
  let firstFrameDocReadable = null;
  const f = document.querySelector('iframe');
  if (f) { try { firstFrameDocReadable = !!(f.contentDocument && f.contentDocument.body); } catch (e) { firstFrameDocReadable = false; } }
  return JSON.stringify({
    injectionRuns: (2 + 2 === 4),          // true means injected JS executes in this frame at all
    canvasCount:   document.querySelectorAll('canvas').length,
    iframeCount:   document.querySelectorAll('iframe').length,
    firstFrameDocReadable,                 // null: no frame. false: frame present, its document walled off. true: readable
    host: location.host
  });
})()
```

Read the result before forming any theory:

- **`injectionRuns` is not `true`, or the call errors:** the substrate is not executing your code in
  this context at all. That is a connection, permission, or pre-flight problem, diagnosed by the
  Pre-flight checks, not by editing selectors.
- **`injectionRuns` is `true` but the region you need is not queryable:** look at `canvasCount` and
  `firstFrameDocReadable`. Content painted to a `<canvas>`, or living inside a frame whose document is
  walled off (`firstFrameDocReadable: false`), has **no per-element DOM you can address** from here.
  querySelector will keep returning nothing however you phrase it, because there is nothing to return.
  That is not a selector to fix, it is a surface class, handled under "When the surface has no reachable
  DOM."

Only after this probe do you have a cause you observed instead of one you assumed. Name the obstacle
from the probe, then pick the remedy.

### 4. Verify by readback

After every write operation: read back the actual DOM state. Visual confirmation is not
enough. React state divergence is a documented silent failure where the UI appears correct
but the application's internal state holds something different.

- For `<input>` and `<textarea>`: read `.value`.
- For `contenteditable` divs: read `.textContent` (plain text) or `.innerHTML` (rich text).
- For recipient address wells in Outlook: after a typed address resolves to a persona pill,
  the email address is gone from every DOM attribute and text node. Only the display name
  remains. Walking the React fiber tree is the only readback method: check `__reactProps$`
  and `__reactFiber$` keys on the pill element, walking up to 8 parent levels. If the walk
  returns nothing, record the recipient as UNVERIFIED, not confirmed. Display name alone is
  not confirmation: the same display name can belong to two different accounts.
- For a surface with no reachable DOM (content painted to a `<canvas>`, or inside a frame you
  cannot read, per "When the surface has no reachable DOM"): there is no node to read, so a *read*
  cannot be a DOM query — verify it by the application's own visible state, an on-screen indicator or
  a screenshot you inspect. The "visuals are not enough" rule above is about not trusting a screenshot
  **over an available DOM read**, because the DOM is the ground truth a canvas cannot show you; when
  there is no DOM read to be had, the rendered surface is the only ground truth for a read, and an
  **unreadable** state is a mismatch, not a pass. A *write* into such a surface is a different matter:
  you never make it as a live keystroke-write (see "When the surface has no reachable DOM" — you build
  to the app's own automation, or STOP), so there is no live keystroke-write to verify here.

If readback shows a mismatch: retry once with an alternative technique (switch from native
value setter to prototype setter, or use the `computer` tool to type manually). If still
wrong after one retry: STOP and report the selector, the intended value, and the actual
value. Do not continue with wrong data in any field.

### 5. Freeze

When a selector and technique are confirmed by readback: add the selector to the SEL table
and the technique to the corresponding step in the script body. Do not defer this step. A
selector tested in isolation but not frozen is not yet part of the artifact.

When all steps are frozen, assemble the full script into the stored artifact file. Freezing
ends the development loop. It does not finish the job: an assembled file that has never run
as a whole is unproven. Proceed to the proving run.

## The proving run

Freezing is not the finish line. Neither is having watched the pieces work during
development. The artifact is proven by one further act, separate from every probe you ran
while developing: execute the stored file, whole, as a single `javascript_tool` call, and
read what that call returns.

This act is also the gate that catches the deepest failure — doing the task by hand instead of
building the artifact (see "Doing the task by hand is the failure this skill exists to prevent"). If
you reach this point and find you have no stored file to execute, that is the answer to the first
question at the top: you did the task by hand and built nothing to prove. There is no proving run
without an artifact, and no "done" without a proving run. Go build the artifact, then come back here
and run it cold.

This is a distinct execution, not a re-description of your development. During development
you ran fragments; here you run the assembled file exactly as it sits on disk, with only the
data block (`ITEMS`/`START_IDX`/`BATCH_SIZE`) prepended, per "execute the frozen artifact
as-is" below. Do this even when every component already worked in isolation: component
verification does not compose into artifact verification. An assembled file can still fail in
ways no fragment test can reveal: a helper closes over a name that no longer exists, two
steps run in the wrong order, a return path is unreachable.

**A change after the proving run unfreezes the artifact.** If proving teaches you something — a
coordinate that needed adjusting, a selector that had drifted, a constant worth recording — and
you edit the stored file to reflect it, the file on disk is no longer the file you proved.
Re-freeze and prove again: the *last* whole-file execution of the run must be of the exact bytes
now on disk. This failure is quiet — the automation works, you tidy the file into its final shape
afterward, and ship a version no execution ever ran. A stored artifact that differs from every
copy you executed is unproven, however well the run went.

**Do not hand-assemble the executed text, and do not vouch for the run in prose. Build the text
mechanically from the stored file, and prove byte-identity with a diff whose empty output is the
proof.** The paragraph above is a rule; a promise to have kept it is not enough, because a promise
and a real check read identically in a report. "I executed the file verbatim" is a sentence, and a
model under load will write the sentence without doing the work behind it, which is exactly how a
stored file ends up differing from every copy that ran. So build the check to leave an artifact you
cannot produce merely by asserting it.

1. **Build the payload with a command, never by hand.** Put only the per-run data block
   (`ITEMS`/`START_IDX`/`BATCH_SIZE`) in its own small file that ends with a newline, then
   concatenate it with the stored artifact to form the exact text you will execute, so the
   file-portion is a copy of the on-disk bytes by construction, with no hand-kept copy in between to
   drift:

   ```bash
   # D = number of lines in data_block.js
   cat data_block.js path/to/artifact.js > proving_payload.js
   ```

   Then Read `proving_payload.js` and pass its exact contents as the `javascript_tool` text; do not
   retype or edit them between reading and executing. (`javascript_tool` runs in the page and cannot
   read your disk, so the code must travel through the call as text, and reading the payload
   immediately before executing it is what keeps "what ran" equal to "what you built.")

   **If the run returns `{}` (or an empty object), the artifact's leading `await` was dropped.** A
   bare `(async function(){...})()` evaluates to a pending Promise, and this REPL serialises that
   Promise as `{}` rather than awaiting it — so `{}` is never "the script found nothing" (a real
   empty result still carries the `artifact` stamp and `completed: 0`); it is the signature of a
   missing top-level `await`. The `await` belongs in the stored file's first line (per the contract),
   so it is inside the byte-identity diff and does not break it.

2. **Prove build integrity with a two-sided diff, and paste the diff.** After the run, strip the
   data block you prepended and compare the remainder against the stored file:

   ```bash
   # empty output = the file-portion you built is byte-identical to the stored artifact
   diff <(tail -n +$((D+1)) proving_payload.js) path/to/artifact.js && echo BUILT-MATCHES-STORED
   ```

   The empty `diff` and the `BUILT-MATCHES-STORED` line prove the payload you built carries the
   stored file byte-for-byte, with no hand-kept copy that drifted; paste this command and its output
   into the report. If `diff` prints anything, the payload you built and the file on disk differ:
   fold whatever proving taught you into the file, rebuild the payload, and prove again. Do not edit
   the stored file after this diff without re-proving it.

   Both operands of this diff are files on disk, so it proves the built payload equals the stored
   artifact and nothing more. It cannot observe what `javascript_tool` does to the text between the
   Read and the execution in the page; that boundary is what step 3 watches.

3. **Prove transfer integrity with a probe, and paste its result.** The step-2 diff is blind to the
   one step with no on-disk copy to compare against: the moment `javascript_tool` carries your text
   into the page. Send a known string through that same transfer and read back what arrived:

   ```javascript
   await (async function () {
     const KNOWN = "transfer-check:alpha|beta|gamma|<<END>>";
     return { probe: 'transfer-integrity', value: KNOWN, len: KNOWN.length };
   })()
   ```

   Compare the returned `value` to `transfer-check:alpha|beta|gamma|<<END>>` as strings, not by eye,
   and paste the returned object into the report as your transfer-integrity evidence. An exact match
   proves the transfer carried this string's interior across byte-for-byte. The transfer is
   content-blind: it normalizes whitespace, not meaning, so a probe that arrives intact is evidence
   the same transfer preserves your artifact's interior too, leaving trailing whitespace as the only
   thing it can drop (inert, because it sits outside every JS token and changes nothing the parser
   sees). That is an inference about a generic transfer, not a byte-for-byte re-measurement of the
   artifact; the probe exists to catch the transfer misbehaving. If `value` differs from the expected
   string in any way, STOP and report it: the transfer is doing something the byte-identity reasoning
   does not cover, and the artifact must not be shipped until it is understood.

A hash of the stored file **alone** is not this proof. Computing the file's `sha256` and stating the
run was "executed verbatim" checks the file against itself and says nothing about the text that
actually ran, which can be a lighter copy while the file's own hash still checks out. Steps 2 and 3
divide the work a hash cannot do: step 2 proves the built payload equals the stored file, killing the
hand-copy drift, and step 3 proves the transfer into the page preserved that payload's interior,
bounding how far the executed text can differ from the stored file to inert trailing whitespace.

Reason: the freeze rule by itself is enforced only by an outside auditor diffing your run against
your file after the fact. Steps 2 and 3 are the two halves of that audit, run by you before you
report and shown in it. Step 2 closes build-time drift, so a stale hand-maintained copy cannot pass
as the artifact. Step 3 closes the execute-time gap the build diff cannot see: it states, in your own
evidence, how far the executed text may differ from the stored one, so an inert difference is
disclosed as inert instead of hidden and a non-inert one stops the run instead of shipping silently.

What a clean proving run returns:

- **Batch or creation workflows (many items):** `completed` equals the number of items
  processed and `errors` is empty, or every error is explained.
- **Single-action workflows (one export, one download, one report pull):** model the single
  action as one item (`ITEMS` of length 1, `BATCH_SIZE` 1). A clean run returns
  `completed: 1` with `errors: []`. The object may also carry workflow-specific fields (an
  exported filename and byte count, say), but `completed`, `errors`, and `artifact` are
  always present. When the deliverable is a file, this object is necessary but not
  sufficient: it proves the script ran, not that a file reached the disk. See "When the
  deliverable is a file on disk."

If one step of the workflow is a control that ignores synthetic events and needs a real
trusted click (the `computer` tool, under Substrate), the proving run is the in-page script
plus that documented `computer` click in sequence; the observed-output object is what the
script's final readback returns after the action completes. State in the report which step
required a trusted click and why.

The return object is the acceptance criterion for whether the artifact executed. Your visual
observation of the page is not, and neither is a directory listing: a file sitting on disk
shows that some download happened, not that this whole-file execution produced it, so it can
never stand in for running the stored file and reporting the object it returns. That guards
against a file listing masquerading as a real run. Its mirror image is the trap that fails
silently: when the automation's deliverable is a file, a clean return object does not prove the
file exists. Apply "When the deliverable is a file on disk" below whenever the deliverable is a
file.

The development report must quote, verbatim under the label "observed output," the JSON
return object of this whole-file execution: the object carrying the `artifact` stamp from the
file's `ARTIFACT_ID`. If what you are about to paste is a directory listing, a file size, a
hex dump, a screenshot, or the output of a probe snippet, you do not have a proving run yet.
Go back and execute the stored file. The task may not be reported complete without the
artifact's own return object.

### When the deliverable is a file on disk

Some automations deliver a file: a download, an export, a report pull that saves a
spreadsheet. For these the in-page return object cannot be the whole proof, because the script
runs inside the page and the page cannot see the filesystem. A script can dismiss the export
dialog, fire the download request, and return `completed: 1` while the browser saved nothing
(see "Gated side effects" below). The return object reports what the page did; it cannot
report what reached the disk.

So a file deliverable makes the proving run two claims, and both must pass:

1. The whole-file execution returned a clean object, quoted as "observed output" above.
2. A new file, the one this run was meant to produce, actually appeared on disk during the run.

Check claim 2 against a baseline recorded before the run, never against the current contents of
the download folder. Pre-existing files, including ones an earlier development attempt
downloaded, are exactly what makes an unbaselined check lie: it reports an old file as today's
success. The portable baseline is a marker file created outside the download folder:

```bash
# before the proving run
touch /tmp/proving_run_marker
# ... execute the stored artifact ...
# after: list only files newer than the marker (adjust the folder to the browser's download dir)
find ~/Downloads -type f -newer /tmp/proving_run_marker
```

A pass is one new file that matches the expected name pattern and type and has a plausible,
non-zero size (a zero-byte file is a failed download, not a deliverable). Report that file's
name and size in the development report, under a label distinct from "observed output": it is
filesystem evidence, not the artifact's return object, and the two are different claims. If no
new file appears, the automation did not do its job whatever the return object said, and that
is an INCOMPLETE to report rather than a gap to smooth over.

This check is yours to run, as the developing agent, in Bash between the pre-run baseline and
the post-run listing. It does not go inside the artifact; the artifact stays a pure in-page
script. When you package a file-deliverable automation, carry the same check into the wrapper's
run-time instructions (see Packaging), because the same false positive reaches production
otherwise.

### When the deliverable is a pipeline (more than one artifact, or more than one surface)

Some workflows are one script. Others are a **flow**: a getter that produces data, a generator that
turns it into a second artifact, a writer that commits it — often across **two surfaces** (read on one
page, write through another app's engine). For these, each artifact's own proving run above is
necessary but **not sufficient**. Green on the getter and green on the writer, proven separately, does
not prove the *flow* runs: the seams between the stages are exactly where a hand-assembled one-off
passes while an honest re-run breaks. Two further claims must hold, and both go in the report.

1. **The data seam is mechanical, not hand-typed.** Where one stage's output feeds the next, capture
   it with a deterministic step — write the getter's returned JSON to the exact file the generator
   reads, in code, the same capture the artifact's user would re-run. **Never hand-transcribe** values
   from a return object into the next stage's input file: that reads as working today and silently
   depends on *you* retyping next time, when the numbers are different. A hand-typed seam is a break in
   "re-runnable," which is the whole deliverable.

2. **One assembled end-to-end pass, proven as a unit.** After the per-artifact proving runs, run the
   **whole flow once on real data** — stage one's real output feeding stage two feeding the final
   commit — and verify the end result by the destination's *own* readout (the same standard as any
   commit: the app's readback or a re-read of the written target, never your intent). Record it under
   its own label, separate from the per-artifact runs. Per-artifact green plus a one-off you stitched
   by hand is not this proof; it is precisely what looked done in every pipeline that later did not
   re-run.

**Multi-surface honesty.** When the flow spans surfaces you bridge — a browser getter and an
app-engine writer — some steps are irreducibly manual at build time: running the in-page getter,
pasting the generated program into the app's editor. *End to end* here does **not** mean one command;
it means the **data** hand-offs are mechanical (claim 1) and the assembled flow is proven once as a
unit (claim 2), with the irreducible manual steps **named in the packaging** so the user knows exactly
what re-running costs and in what order. Do not fake a single-command pipeline the surfaces cannot
support — state the real shape instead.

### Gated side effects: when a synthetic click is not enough

The `simulateClick` helper dispatches the full pointer and mouse sequence, which is enough to
trigger the JavaScript handlers most controls attach. Downloads are different. A browser
performs a script-initiated download (an anchor click, a `window.open`, a navigation to the
file) only when it can attribute the action to a genuine user gesture. A synthetic event has
`isTrusted === false`, so the handler runs, telemetry fires, the export dialog closes, and the
network request for the file may even go out, yet the browser suppresses the save itself. The
page looks like it worked and nothing reached the disk. That is the precise shape of a
false-positive proving run: a clean return object over a file that never existed.

Two remedies, either of which yields a real file:

- **Trusted click.** Make the final download-triggering click with the `computer` tool, not
  `simulateClick`. A `computer` click carries `isTrusted === true`, so the browser honors the
  download. This is the trusted-click step described earlier; a download is one of the actions
  that needs it.
- **Capture and fetch.** If the script can obtain the file's URL (from the export request, a
  link `href`, or by intercepting the navigation the handler attempts), fetch that URL in the
  page and save the response as a blob. This keeps the download deterministic and inside the
  script. It only works when the URL is reachable that way, and some signed URLs are single-use
  or expire, so verify the file afterward regardless.

Whichever you choose, the file-on-disk check above is what closes the loop. A trusted click can
still be defeated by a popup blocker; a fetched URL can still return an error. The click is the
mechanism; the new file on disk is the proof.

### When the surface has no reachable DOM — don't drive it live; find the app's own automation, and STOP only if there is none

"Gated side effects" above is about one control that rejects synthetic events. Sometimes the
obstacle is larger: the whole region you must read or write has no per-element DOM at all, because it
is painted to a `<canvas>` or lives inside a cross-origin frame you cannot read — an out-of-process
iframe (OOPIF). The obstacle probe under "Test in isolation" confirms it: `canvasCount` over the
target region, or a frame whose `firstFrameDocReadable` is `false`. **Excel / Google Sheets /
Office-on-the-web grids are the archetype** — a canvas grid inside a cross-origin OOPIF, with no cell
DOM to address and none to read back.

**Do not drive such a surface live.** The tempting move — a person-imitating stream of trusted
`computer` keystrokes aimed through the app's own affordances (a go-to / Name-Box field, arrow keys)
and steered by screenshot — is not an automation, and it fails on the two axes that matter:

- **It is not a deterministic artifact.** With no DOM an in-page JS write is impossible, and a
  cross-origin OOPIF walls JS injection anyway. A screenshot-steered keystroke stream is the model
  hand-operating the app live, every run, with no stored thing that works without it — the exact
  hand-doing the done-gate forbids.
- **Driven live it is slow, screen-stealing, and quietly wrong.** The model works
  screenshot → reason → click at seconds per action, and because it must *see* the canvas to place
  each keystroke it holds the window frontmost the whole time — taking the person's screen for the
  entire operation, not a brief moment. Worse, the errors hide: on a spreadsheet grid a single `type`
  of `"a⇥b⇥c"` can land as literal tab characters inside one cell, and a clipboard readback of that
  cell returns `"a\tb\tc"` — which *looks* like three populated cells and passes a naive check while
  the write is actually broken. A surface whose own verification can lie is not one to automate blind.

So live-driving is out. But that does **not** make the write out of scope by default — it means you
have not yet found the right door.

**Before you conclude STOP, look for the app's own automation — its "machine door."** Complex
applications that hide their content behind a canvas or an OOPIF very often expose a first-party way
to command them that is *not* clicking their UI — precisely because their UI is hard to script: a
script or macro engine, a formula/query surface, a bulk import or paste-special, a documented add-in.
That automation runs *inside the application itself*, reached through its own UI in the browser — so
it is neither driving the canvas live nor a prohibited tool-switch to an external API or connector
(the machine door is the app's *in-browser* automation; a Graph/REST call or an MCP connector is the
off-browser side channel barred under "The browser is the only surface"). It is the app's own door,
it is deterministic, and it is the artifact you build to.

The general test — ask it of every hard surface before you give up: **is there a way to tell this
application what to do that is not simulating a human clicking it?** To find it, probe the app the way
you probe any surface: scan its menus, ribbons, command palette, and settings for the words this
class of feature hides behind — *automate, script, macro, record, add-in, developer, import, query*.
You are looking for a door the app opens for machines. (This is not a spreadsheet-specific trick: it
is the first question to ask of any surface whose DOM you cannot drive. The specific door differs per
app, and finding it is your job, not something this skill can pre-name for you.)

When you find one, build to it. The pattern generalizes from the script contract: the app's
automation usually cannot take external arguments when you run it from the app's own UI, so you
**generate its program deterministically** — a stable template plus a data block filled from your
headless getter — the in-app equivalent of injecting `data_block.js`. The generated program is the
artifact (logic and data separate at build time, combined only in the emitted text); running it in
the app is the proving run, and the app's own output channel (a results pane, a console line, a
returned cell) is the return object you read back and quote. Keep the emitted program self-guarding:
it writes exactly the intended target and throws rather than touch anything else. Only the
authoring/running step needs the screen; everything before it — the getter, the generator, the
emitted text — is headless, so the foreground shrinks to one brief commit instead of the whole job.
That one commit is **mandatory, not optional, and not a defeat**: you paste and run the program
through the app's editor UI, which takes the screen for that moment. There is **no headless path into
a cross-origin OOPIF editor** — no RPC, no `postMessage`, no command-bus call — so do not go hunting
one. Reverse-engineering the app's internal messaging or fetching its script bundles to avoid opening
the editor is out of scope and is the live-fumble failure wearing a headless disguise: once the
generated program exists and only paste-and-run remains, stop probing and take the screen.

**The brief foreground commit has its own traps — know them before you take the screen.** Confirm real
input focus first, and do not trust `document.hasFocus()` alone: on a cross-origin OOPIF it can read
`true` as *emulated top-document* focus while keystrokes still fail to cross into the child frame — the
mouse click crosses (hit-testing is browser-process level), the keystroke does not unless the window is
genuinely frontmost. The **first click into an OOPIF surface is a focus event, not an action** — click,
confirm from a screenshot that the focus or selection actually moved, then type. And verify the commit
by the app's *own* readout, never by your intent — a results pane, a console line, a figure the app
computes for you (a grid's **Count / Sum / Average**, say) — because a keystroke stream that only
half-landed leaves a screen that *looks* right while being wrong.

**Bring the window frontmost yourself — that is your job, not the user's.** Keystrokes only land in the
OS-frontmost window, and the in-page routes do not get you there: `window.focus()`, `resize`, and any
page-initiated focus are silently ignored — the browser blocks a page from stealing the OS foreground.
You need an OS-level activate, and it must target the automation's **own dedicated window**, never the
window the person is working in. The proven path on macOS is `osascript` (AppleScript) to activate
Google Chrome and raise the specific window; two gotchas make it fail *silently*, so know them: it
needs an **unsandboxed shell** (the Bash sandbox blocks the Accessibility call with no error at all),
and the terminal needs macOS **Accessibility permission** (if the command runs clean but the window
does not come forward, that missing permission is why). Disambiguate when several Chrome windows exist
so you raise yours, then **verify it actually came forward** with a screenshot before you type — same
rule as the first click: confirm the state changed, do not assume it. On a non-Mac platform, find the
equivalent OS activate — a foreground/activate call for that OS's window manager exists everywhere —
and prove it the same way. Only if you genuinely cannot find or run any self-activate path do you fall
back: tell the user plainly, ask them to bring the window frontmost, and wait. Self-raise is licensed
for this one necessary commit only — never pre-emptively, never "to be safe," never onto the user's
working window.

**When you cannot *see* a surface you need, self-raise is the fix — reach for it before any API.** A
blank screenshot on a hidden background tab is not a walled surface and not a blocked path: it is an
unraised window. If you need to see or drive a surface and it is blank because its tab is not
frontmost, **raise it and look** — do not reach for a REST, Graph, or other off-page API to route
around not being able to see the page. That reach is the barred tool-switch below, and here it is
triggered by a problem one self-raise call solves. Order the ladder so the in-bounds move (activate
the window) always comes before the out-of-bounds one (an API that reaches the data another way).

**STOP is the last resort, not the first.** It is correct only when the surface has **no reachable
DOM to write and no app-native automation door** — genuinely no in-browser way to command it
deterministically, confirmed by an actual search for one, not assumed. Then STOP as you would for a
CAPTCHA: name the surface plainly ("the grid is a canvas inside a cross-origin frame — no per-cell
DOM, and no script/macro engine I could find after checking the ribbon, menus, and command palette"),
state what you verified and what you searched, and give the user their real options — do this step by
hand, or use the application's API outside this automation. A clear stop is a success; the two
failure modes are the half-hour live fumble and the premature stop that never looked for the machine
door.

**Reading a no-DOM surface has its own machine door — reach for it before you conclude the content is
unreadable.** A live canvas cell gives you nothing headless: there is no DOM to query, the clipboard
does not cross a cross-origin OOPIF while the window is occluded, network capture returns no cell
bodies, and a screenshot of a background tab is blank — so reading a cell *live* forces the screen,
and OCR is not deterministic. But the content almost always *also* exists as a **file the application
will hand you**: its own download or export — the same button a human clicks to get the document —
served by the app's authenticated session in the browser. Fetch that file with the session
(`fetch(downloadUrl, {credentials:'include'})`, or trigger the app's own download), save it, and read
it with a local deterministic parser (a spreadsheet library, or just unzip the container — an Office
`.xlsx`/`.docx` is a zip of XML). You get the true values headless, before you ever take the screen.
This is the *read* counterpart to the automation engine above: **download-and-parse to read, the
automation engine to write** — and on a canvas surface the read comes first, so the write is driven by
real data instead of a guess matched off screen pixels. Two rules ride with it, both non-negotiable.
(1) It is *in-browser*: the file is fetched by the app's own logged-in session, which is the machine
door — not the barred external Graph/REST/MCP tool-switch. The line is *who authenticates*: the
browser's own session, not a token you carry in from outside. (2) The download is the *whole* document
— every sheet, row, and field, including data your task has no business seeing — so parse **only** the
authorized range, surface nothing else, and delete the downloaded file when done. Only when the app
offers no download, export, or other readable channel at all — content that exists solely as canvas
pixels — do you STOP and report reading as unreachable, the same way.

**Doing it — the steps that stump an unwarned agent.** Finding the file's URL is the real work, not
fetching it. The download/export control is usually a *reachable* element even when the content is
canvas — a Download / Export / *Save a copy* entry in a menu, ribbon, or toolbar — so read its URL
from the DOM, or invoke it once and catch the file request in `read_network_requests`; discover the
endpoint, do not assume it. `javascript_tool` runs *inside the page* and cannot write your disk, so
land the bytes one of two ways: build a Blob from the fetched `arrayBuffer` and click an
`<a download="name.ext">` (it saves under a filename you choose, which you then read from disk), or
return the bytes base64 and write them yourself (a large return may be truncated — chunk it, or prefer
the download route). The tool also **redacts any result carrying a URL, token, or cookie** (you get a
`[BLOCKED …]` result), so never return the download URL or `location` — return only non-sensitive
proof: a byte count, the file's magic bytes, the parsed authorized values. Confirm you fetched the
*file* and not a `200`-with-an-HTML-login-page before you parse: check the content-type and the
leading magic bytes (a zip / Office file starts `50 4b 03 04`). And parse with a real library where
one exists — the container has indirection (a shared-string table, a sheet-name-to-file map) that a
hand-rolled unzip has to reproduce.

## Build-time vs run-time

**Build-time** (this skill's domain): interactive, iterative, exploratory. You probe,
revise, and retry. Selector failures are expected. Ends at the proving run.

**Run-time** (the generated artifact's domain): deterministic. The frozen script runs;
any selector failure means the page has changed since development. Every run-time rule must
be **embedded in the generated artifact** (script header plus any wrapper skill the user
creates), because no link back to this plugin exists when the artifact runs.

Run-time rules to embed in every artifact:

- A SEL selector returning null: STOP and report which selector failed.
- Partial mismatch (some SEL selectors resolve, others do not): also STOP. Partial is not
  partial success; it means the page structure changed.
- A write target with no reachable DOM (a `<canvas>` region or a cross-origin OOPIF — an
  Office/Sheets-style grid — per "When the surface has no reachable DOM"): never driven live by the
  `computer` tool. If the app has a machine door (a script/macro engine or equivalent), the artifact
  is the program you generate for that door, and its run-time STOP conditions are that program's own
  embedded guards (target-missing, shape-mismatch — thrown by the emitted program). If the surface
  has no such door, STOP and report: name the surface and hand the write back to the user (by hand, or
  via the application's API outside this automation). Either way, an artifact must never attempt a
  live `computer`-driven write into a no-DOM surface.
- CAPTCHA or challenge dialog: stop immediately, no retries, 30+ minute rest, the user owns
  the clock. Do not resume until the user confirms the account is clear.
- Forbidden controls: listed by selector in the SEL table with DO_NOT_CLICK written next
  to them.
- Tab state: keep the automation's tab the active tab of its window, and the window
  un-minimized. A background (non-active) tab is not painted, so a screenshot of it is blank,
  and its in-page timers are throttled. Window focus and stacking do not matter.
  `document.hidden` is not a capability signal.
- Execute the frozen artifact as-is: pass the stored file contents without stripping
  comments or reformatting. A claim that the artifact was run unmodified may only be made
  when the executed text consists of the data block (ITEMS/START_IDX/BATCH_SIZE) followed
  by the stored file contents, with the file portion byte-identical to the stored file up to
  inert trailing whitespace: the file-to-`javascript_tool` transfer deterministically trims a
  single trailing newline, so that one difference is expected and permitted, but any interior
  difference (a changed, added, or removed non-whitespace byte anywhere before the trailing
  whitespace) is a modification, and you STOP and report. The step-3 transfer-integrity probe
  is what catches a transfer doing more than trimming trailing whitespace. Prepending the data
  block is the documented execution pattern and does not count as modification.
  Reason: the header comments are the contract; an executed copy with them removed cannot
  be audited against the file, and "unmodified" claims that mean "logically equivalent"
  force every auditor into a diff they should not need.

### Packaging

When the proving run returns a clean result object, ask the user:

1. Where should the script file live? (Their skill directory, or a working folder?)
2. Where should the data pipeline go? (The script that produces the JSON input)
3. Where should the run log go?

Create the files at those paths. If the user wants a wrapper skill, the wrapper must
include: what the script does, prerequisites (login state, open tab, required files), the
run-time STOP rules (selector failure, CAPTCHA protocol, forbidden controls), and
instructions for calling the script via `javascript_tool` with the data injected at the
top. The wrapper is the artifact that travels with the user. Make it self-sufficient.

**File-deliverable automations, at run-time:** if the automation's deliverable is a file, the
wrapper's run-time instructions must tell the running agent to confirm a new file appeared
after each run (matching name, type, and a non-zero size), and to treat a clean return object
with no new file as a failure to report, not a success. The artifact's return object is
produced inside the page and cannot see the disk, so without this the same false positive a
proving run catches at build-time would pass unnoticed in production.

**Return object labels in reports:** every return object shown in a development report
carries one of two labels, "observed output" (produced by a run that actually executed)
or "example" (a constructed illustration). An unlabeled return object is treated as an
example and proves nothing about the artifact's behavior. The "observed output" label is
valid only for an object carrying the `artifact` stamp produced by executing the stored
file whole (see "The proving run"). Filesystem metadata, a screenshot, or the output of a
probe snippet is not "observed output" no matter how it is labeled.

**Side-effect disclosure:** the development report must include a side-effects section
listing any interaction with pre-existing user data: opening, focusing, selecting, or
otherwise touching real messages, documents, or records that existed before the run.
Passive DOM reads (querySelector probes, innerText polling) do not count as interactions;
the rule covers user-visible interactive actions on pre-existing data only. This covers
only pre-existing data, not artifacts the run itself created; without this guard, every
created draft becomes a "side effect" and the section drowns in noise.

## Safety rules

These apply in both build-time and run-time phases. Embed them in every generated artifact.

### The browser is the only surface — a blocked path is a STOP, never a tool-switch

Everything this skill builds runs through the Claude-in-Chrome tools, and only those. When the
browser path is hard — a control that will not take input, a canvas that will not read back, a
surface that resists every technique here — the correct move is to diagnose it with the probes in
this skill and, if after that diagnosis it genuinely cannot be driven from the browser, to **STOP
and report**. It is never to reach for a different tool that reaches the same data another way: not a
REST or Graph API, not a Microsoft 365 / SharePoint / Google / Drive MCP connector, not a
download-edit-reupload of the underlying file, not a local library (openpyxl, a CSV rewrite) that
edits it off-browser. Switching tools to get the write done is prohibited, and the prohibition is
absolute — it holds even when the other tool is right there, already authenticated, and would plainly
work. Four reasons, each sufficient on its own:

- **It is out of scope.** This skill develops *browser* automations. The browser is the surface the
  user asked to automate. A script that writes through an API instead of the page is a different
  automation than the one being built, however similar its output looks.
- **It does not transfer — which makes the "success" false.** The other tool works because *this*
  machine happens to have that connector authenticated. The person the automation is for, on their
  own install, has no such connection; the artifact that leaned on it does nothing for them. A run
  that "succeeded" through a private side channel reports success for a deliverable that cannot run
  anywhere else — the precise failure this whole skill exists to prevent: a result that looks like it
  works and does not carry.
- **It reaches around the safety guards, not through them.** The in-browser path is guarded cell by
  cell, click by click, with readback and confirmation. A whole-file API PATCH or a
  download-edit-reupload replaces or rewrites the document wholesale, past every one of those guards —
  the exact structural damage ("never touch another cell, never change the document's structure") the
  rules forbid, entered through a back door.
- **It voids what the run is for.** The point is to learn whether the *browser* path works. A shortcut
  through another tool answers a different question and leaves the real one unanswered.

A blocked browser path, correctly diagnosed and reported, is a **good** outcome — an honest INCOMPLETE
that tells the user the truth about their surface. A "success" bought by switching tools is
worse than useless, because it hides that truth behind a deliverable that will not run for them.

### When blocked and waiting for the user, wait

If you are blocked on something only the user can do — confirm a state, clear a challenge like a
CAPTCHA, or bring the window frontmost *only when you have already tried and failed to self-activate
it* (see "The brief foreground commit" above) — say so plainly, then **wait for it**. Do not fill the wait by exploring an
alternative approach, loading another tool "to be productive," or testing a different write path in
the meantime. That idle-time detour is exactly how the tool-switch above gets started: the block was
temporary and user-resolvable, but the wait got filled with a hunt for a way around it, and the way
around it was out of bounds. Waiting is the correct action, not wasted time — the user resolving the
block is the path forward, and there is no faster one worth breaking scope for.

### Destructive actions

**Never click Send, Submit, Publish, Delete, or Discard** without explicit prior user
consent for that specific action in the current session. "Create drafts" does not mean
"send emails." Discard is listed by name because it sits adjacent to Send on compose
surfaces and destroys the work just built. Consent for one action in one session does not
carry over to another action or another session.

The forbidden button selectors for Outlook are in `${CLAUDE_PLUGIN_ROOT}/scripts/browser/dom_patterns.json`
(at the plugin root) under `outlook_office.send_button_DO_NOT_CLICK` and `outlook_office.discard_button_DO_NOT_CLICK`.
Read that file to get the current multi-language selector list before building your SEL
table. For other platforms, derive the forbidden selectors from the live aria-label
enumeration probe and add them to SEL with DO_NOT_CLICK labels.

### CAPTCHA protocol

When any CAPTCHA or challenge indicator is detected:

1. STOP immediately. Halt all automation on this platform.
2. NEVER retry, even if the CAPTCHA appears to clear itself.
3. Tell the user: "Nothing may resume on this platform for at least 30 minutes. I have no
   timer and cannot track this interval: you must decide when to resume."
4. Before resuming: check the account for warnings or restrictions. On LinkedIn, visit the
   notification center and watch for restriction emails. On Outlook, ask the user to check
   their admin center for flagged activity. Do not resume until the user confirms it is
   safe.

### LinkedIn limits

Minimum 3-second gaps between page loads. Maximum 50 data points per session. Warn the
user before starting any LinkedIn automation. An account restriction requires a manual
appeal and can take days to resolve. It is the most damaging failure mode in this plugin.

### Structure mismatch at run-time

A SEL selector returning null at run-time means the page changed since development. Stop.
A partial match (some selectors resolve, others do not) also means stop. Report which
selectors failed and which resolved. Do not attempt any action after a mismatch.

### Keeping the tab drivable

The automation's tab must stay the active tab of its browser window, and that window must not
be minimized. Chrome does not paint a background (non-active) tab, so a screenshot of one comes
back blank, and it throttles a background tab's in-page timers. Neither has a workaround from
inside the automation; both are avoided by keeping the tab active.

Window focus and stacking do not matter. An unfocused window, behind other windows or behind
another application, is driven and captured normally, so the user can work in other apps and
other windows for the whole run. Only this tab's own window must stay un-minimized with this
tab active.

Do not gate on `document.hidden` or `visibilityState`. That signal reports page visibility (tab
selection and window state), not whether the tab can be captured or scripted; treating it as a
capability check causes false stops. Judge drivability by the one thing that proves it: a
screenshot that returns real pixels.

## Worked examples

`${CLAUDE_PLUGIN_ROOT}/REFERENCE.md` contains worked examples for two workflow families:

- **LinkedIn Analytics extraction**: covers the probe loop on a page with obfuscated class
  names (keyword selectors return empty arrays), the correct analytics URL (an older version
  redirected silently to the feed), and the `main.innerText` technique as a last-resort that
  produces an INCOMPLETE deliverable (not the contracted extractor), usable only after
  documented exhaustion of every strategy in the selector preference order.
- **Outlook Web Drafts creation**: covers the contenteditable To field and why value setters
  fail on it, autosave polling as the only safe save mechanism (Ctrl+S can trigger Chrome's
  native Save dialog and block all further automation; the compose surface may have no close
  button at all), domain redirect handling, and selector discoveries and failure modes for
  compose surfaces including the forbidden Send and Discard buttons.

The examples show the non-obvious discoveries and real failure modes from live test runs.
They teach the development loop on real cases. They do not give you selectors to copy:
selectors from the examples need re-verification against your live page. The failure modes
are stable lessons about these platforms' behavior.

Read the examples before your first run on either of those targets. For a different target,
use the development loop in this skill. The examples show what the loop looks like in
practice on hard targets.
