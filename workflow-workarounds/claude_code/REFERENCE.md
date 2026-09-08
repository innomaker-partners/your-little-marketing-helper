# Workflow Workarounds - Reference

## Endpoint Registry (Platform Connector)

This registry holds the operations verified for the two worked-example providers. It is a starting set, not the tool's limit: when you wire a new provider (see "Adding a new provider" in the platform-connector skill), add its operations here as rows, so calls are checked against a known-good list.

| Operation | Platform | Endpoint | Method | Required params | OAuth scope | Response shape |
|---|---|---|---|---|---|---|
| `calendar.events.list` | microsoft | `/me/calendar/events` | GET | `startDateTime`, `endDateTime` | `Calendars.ReadWrite` | `{value: [{subject, start, end, ...}]}` |
| `calendar.events.create` | microsoft | `/me/calendar/events` | POST | `subject`, `start`, `end` | `Calendars.ReadWrite` | `{id, subject, ...}` |
| `files.list` | microsoft | `/me/drive/root/children` | GET | optional `path` | `Files.Read` | `{value: [{name, size, ...}]}` |
| `files.download` | microsoft | `/me/drive/items/{id}/content` | GET | `item_id` | `Files.Read` | binary/base64 |
| `mail.list` | microsoft | `/me/messages` | GET | optional `$filter`, `$top` | `Mail.Read` | `{value: [{subject, from, ...}]}` |
| `ig.media` | meta | `/{ig-user-id}/media` | GET | `fields` | `instagram_basic` | `{data: [{id, caption, ...}]}` |
| `ig.insights` | meta | `/{ig-user-id}/insights` | GET | `metric`, `period` | `instagram_manage_insights` | `{data: [{name, values, ...}]}` |
| `page.insights` | meta | `/{page-id}/insights` | GET | `metric`, `period` | `pages_read_engagement` | `{data: [...]}` |

**`/me/` limitation**: These endpoints use delegated permissions: they return data for the signed-in user only. For shared mailboxes, use `/users/{email}/messages` instead (requires `Mail.ReadWrite.Shared` scope). See the Platform Connector SKILL.md for guidance.

**Meta ID discovery**: `ig-user-id` and `page-id` are opaque numeric IDs not visible in the Instagram/Facebook UI. After OAuth setup, call `/me/accounts` via Graph API Explorer to get Page ID, then `/{page-id}?fields=instagram_business_account` to get IG User ID. The SKILL.md pre-flight walks through this.

## Worked Examples (Browser Automation)

These are worked examples of the development loop described in the browser-automation SKILL.md.
They exist to show what the loop looks like on hard targets: specifically, the discoveries an
agent would not arrive at from first principles, and the failure modes seen on live pages. They
are not recipes to replay step by step.

The empirical details here (URLs, selector values, label strings) were true on the observed tenant
and date and are expected to change as platforms update. The method is what transfers to pages you
have never seen. Re-verify all specifics against your live page before freezing them into a SEL
table.

### LinkedIn Analytics (extraction family)

#### Anti-pattern: guessed class selectors return empty

A class-keyword selector -
`[class*="metric"], [class*="analytics"], [class*="stat"]` - is the conventional first guess. On the
live analytics page (`https://www.linkedin.com/analytics/creator/content/`), it returned `[]`. LinkedIn's build
pipeline replaces semantic class names with obfuscated content hashes: the observed DOM carried
names such as `_04113bfd f9d54e6d`. No human-readable keyword appears in any class attribute on
the page.

This is the clearest illustration of why the script contract requires empirically harvested
selectors, never guessed patterns. A class-keyword selector is conventional for static sites.
LinkedIn is not a static site, and the framework probe result (below) makes that easy to miss.

#### Framework probe returns "static" and misleads

The framework detection probe returned `{ react: false, angular: false, vue: false }` on LinkedIn.
LinkedIn hides its React devtools hook, so the probe cannot detect React. The result is technically
correct for what is observable. The problem: a "static" result implies that standard CSS selectors
are stable, which is the opposite of true here. An agent building class-keyword selectors after
seeing a "static" result will get `[]` with no signal about why.

Treat a "static" probe result as "framework undetected," not as "stable selectors." On any large
social platform or modern analytics dashboard, do not infer selector stability from a "static"
probe result. Probe the live DOM to discover what selector strategies actually work.

#### Dead URL and silent redirect

The obvious analytics URL (`https://www.linkedin.com/analytics/`) does not resolve. It returns
"This page doesn't exist" and immediately redirects to `https://www.linkedin.com/feed/`. The
navigation call returns success, the CAPTCHA probe clears, and only an explicit page-state check
reveals the wrong page. Extraction against the feed DOM returns nothing and raises no error.

The correct URL is `https://www.linkedin.com/analytics/creator/content/`.

When that URL fails and the agent lands on the feed, the correct URL is recoverable from
the feed page itself: the "Post impressions" link in the left sidebar carries
`href="https://www.linkedin.com/analytics/creator/content/"`. No additional navigation needed.

Always check the actual landed URL and body content for error markers after every navigation. A
redirect gives no HTTP error; the navigation call returns success regardless of where the browser
lands.

#### `main.innerText` as last resort, not substitute for an extractor

After the class-based selectors returned nothing, reading
`document.querySelector('main').innerText` from the correct analytics URL retrieved all headline
metrics, discovery breakdown, engagement counts, and post-level data. LinkedIn's analytics page
renders text in a predictable label-then-value order, making the extracted text parseable
downstream.

This technique is a last-resort fallback, not a substitute for a deterministic extractor. A script whose extraction rests on `main.innerText` is an INCOMPLETE deliverable and must be reported as such, with the documented exhaustion evidence. The user then decides whether to accept the degraded artifact. It does not count as the contracted extractor by default.

Structural selection is exhausted only when the agent has attempted and documented, for each target element, every strategy in the selector preference order: (1) aria-label anchored selector, (2) role plus attribute combination, (3) tag plus stable attribute, (4) structural landmark with a child path (such as `main > [stable-child-selector]`). The task report and the script header must list each strategy that was attempted, the selector used, and the result returned, before any `main.innerText` use is accepted.

#### Rate limits

3-second minimum between page loads, 50 data points maximum per session. These are safety
constraints, not suggestions. A LinkedIn account restriction requires a manual appeal and can take
days to resolve: it is the most damaging failure mode in this plugin. See the safety rules in
`skills/browser-automation/SKILL.md`.

---

### Outlook Web Drafts (creation family)

#### Pre-condition: Conditional Access re-authentication

If a Microsoft Entra ID Conditional Access re-authentication prompt appears on navigation, the
agent cannot proceed. Clicking a login form submit button is classified under the same rule as
clicking Send, Delete, or Discard: it is an irreversible action requiring explicit per-session
user permission. Even when the browser has auto-filled credentials, the agent may not click Submit
without that permission.

The correct pre-condition is not "navigate and see if the inbox loads" but "confirm the user has
manually authenticated before dispatching the agent." A pre-filled login page is not a cleared
pre-condition.

#### Domain redirect

Navigating to `outlook.office.com` redirects to `outlook.cloud.microsoft`. Any domain guard that
checks for `outlook.office.com` and treats any other domain as a stop condition will abort on a
fully authenticated Microsoft 365 session. Check the actual domain after the redirect settles,
not the URL navigated to.

Site permissions for the Claude-in-Chrome extension must cover both domains. See the pre-flight
checks in `skills/browser-automation/SKILL.md`.

#### To field is a contenteditable div, not an input

The recipient well is `div[contenteditable="true"]`, not `<input>`. The React controlled-input
technique (native value setter via `HTMLInputElement.prototype`) does not apply to div elements:
there is no `.value` property on a div. An attempt on the To field fails silently: no error is
raised, no recipient is filled.

The correct technique is `ClipboardEvent('paste')` with a `DataTransfer` object. This is the
`pasteIntoContenteditable` helper in the script contract in `skills/browser-automation/SKILL.md`.

#### Recipient readback requires React fiber walk

After typing an address into the To field, Outlook resolves it into a persona pill that shows only
the contact's display name. The email address disappears from every attribute and text node in the
DOM. Display name alone does not confirm the right recipient: the same display name can belong to
two accounts belonging to one person (personal and work).

The only readback method is walking React's internal fiber tree: check `__reactProps$` and
`__reactFiber$` keys on the pill element and its ancestors, up to 8 parent levels. The readback
section of `skills/browser-automation/SKILL.md` has the implementation. If the walk returns
nothing, record the recipient as UNVERIFIED. Do not report a recipient as confirmed from the pill's
display name.

#### Saving: autosave only, no close button, Ctrl+S is a trap

When compose is docked in the reading pane, there is no close button on the compose surface. The
only dismissal controls are Send and Discard, both forbidden. Ctrl+S does not reliably save in
Outlook Web: if the page does not intercept it, Chrome opens its native "Save page as" modal, a
blocking dialog that requires human dismissal and stops all further automation.

Outlook autosaves the compose window a few seconds after the last edit and shows a confirmation
string in the page: `Draft saved: HH:MM` (on the observed tenant, Hungarian:
`Piszkozat mentve: HH:MM`). Poll for that string and treat it as the save confirmation. After it
appears, navigate away by clicking a folder in the folder tree: the draft persists in Drafts with
no dismiss click.

#### Tenant-variable aria-labels

Microsoft ships different aria-label strings for the same control to different tenant
configurations. The compose button was observed as `button[aria-label="Új üzenet"]` in one
source and as `button[aria-label="Új"]` on the live tenant ("Új" and "Új üzenet" both translate
as "New" or "New message" in Hungarian). Full-string matching fails when the tenant's label
differs.

Use prefix matching: `button[aria-label^="Új"]`. Before building any SEL entry for an Outlook
control, enumerate the toolbar's actual aria-labels from the live page using the aria-label probe
in `skills/browser-automation/SKILL.md`. Do not carry labels over from one tenant to another.

The Hungarian examples above are concrete evidence of the general principle, not a
Hungarian-specific concern: any tenant can produce a label that differs from what another tenant
shows. Harvest from the live page; match by prefix for any label that may be abbreviated or
differently phrased across configurations.

#### Generic contenteditable selector ambiguity

The generic selector `[role="textbox"][contenteditable="true"]` is not unique on a compose
surface. Both the message body and the recipient well are contenteditable elements, and both may
carry `role="textbox"`. Whichever appears first in document order is what the generic selector
returns.

If the body selector matches the To field instead, the ClipboardEvent paste puts the message body
content into the recipient line. No error is raised. The draft's To field contains the email text;
the body field is empty.

Anchor the body selector on its aria-label. Anchor the To selector on its aria-label. Do not use
`[role="textbox"]` alone as a unique identifier for either.

---

### Recurring failure modes

These patterns appeared in both examples and apply to any target.

**Silent redirects prove nothing about the page.** A navigation call returns success regardless of
where the browser lands. A redirect gives no HTTP error. Checking the actual landed URL and body
for error markers after every navigation is not optional: an empty query result is
indistinguishable from a wrong-page result without it. (LinkedIn: the feed page returns `[]` from
the analytics selectors with no indication of the problem. Outlook: the `office.com` to
`cloud.microsoft` redirect invalidated a domain guard silently.)

**A "static" probe result does not imply readable selectors.** The framework probe detects what is
observable. A platform that hides its devtools hook returns "static." This does not mean CSS class
names are semantic or stable. Build-time selector failure after a "static" result means revise the
selector strategy, not conclude that the page is well-structured for class-keyword selection.

**Verify against the DOM and written criteria, not against appearances or agent messages.** Two
documented failures: a recipient recorded as verified from a display name on a persona pill (the
address was gone from every DOM node); a run graded DONE from the impression that a draft existed
and metrics were extracted, not from checking the written success criteria line by line. Readback
means reading actual DOM state. A pass verdict means checking each criterion in writing.

**At run-time, a null selector is a STOP, not a probe-and-revise.** Build-time and run-time have
opposite responses to a null result. At build-time, null means revise the selector hypothesis and
keep iterating. At run-time, null means the page changed since the script was frozen. Improvising
a workaround at run-time produces output downstream of a broken step and makes it impossible to
determine whether the step is sound. Stop and report which selector failed.

**Virtualized-list item counts precede DOM rendering.** After creating items in a folder,
the folder counter readable from `document.body.innerText` updates before the virtualized
message list renders any `[role="option"]` items. Verify item counts via the counter first
(fast); then poll for the item list once it renders (a generous timeout is needed). A
screenshot is the last resort when the list does not render within the timeout; its use
must be stated as a limitation in the report. (Observed in Outlook Drafts: the counter
read 17 while `[role="option"]` returned 0 items for over 8 seconds.)

## Error Reference

### Platform Connector

These match the `diagnosis` field returned by `platform_request.py`:

| `diagnosis` value | HTTP status | Cause | Fix |
|---|---|---|---|
| `share_token_mismatch` | 401 | Token in `.env` doesn't match Make.com scenario variable | Check that the `SHARE_TOKEN` variable in Make.com scenario settings (gear icon → Variables) matches the value of `MAKE_SHARE_TOKEN` in your `.env` file |
| `oauth_token_expired` | 401 | Platform OAuth token expired inside Make.com | Open Make.com → scenario → click the failing module → Connection → Reauthorize |
| `missing_permission_scope` | 403 | Required API permission not granted | Check the scope column in the Endpoint Registry above. Azure AD scopes may need admin consent. |
| `malformed_request` | 400 | Wrong parameters, bad date format, misspelled field | Run with `--verbose` to see the full request body and platform error message |
| `rate_limited` | 429 | Too many requests to the platform API | Wait a few minutes and retry. Check platform-specific rate limit docs. |
| `operations_limit_exceeded` | varies | Make.com free tier monthly limit hit | Upgrade Make.com plan or wait for monthly reset (resets on billing date) |
| `connection_failed` | 0 | Webhook URL unreachable, DNS failure, or network issue | Verify `MAKE_WEBHOOK_URL` in `.env`. Check Make.com status page. Run `--test` to isolate. |
| `http_NNN` | NNN | Unmapped error | Run with `--verbose` for full response body. Consult platform API docs. |

### Browser Automation

| What happens | Cause | Fix |
|---|---|---|
| `tabs_context_mcp` call fails or returns no tabs | Claude-in-Chrome extension not installed or not configured as MCP server | Install extension, then `claude mcp add claude-in-chrome`, restart Claude Code |
| "Permission denied" or "Cannot access page" | Site-level permissions not granted in extension | Open Claude-in-Chrome extension settings and add the target domain (e.g., `linkedin.com`) |
| Selector returns null at build-time | Expected during development; probe and revise | Re-probe the live DOM, revise the selector hypothesis, and test again |
| `javascript_tool` returns `{}` from an async IIFE | Unawaited Promise: `javascript_tool` evaluates code as an expression; a plain `async function(){}()` returns a Promise object, not the resolved value | Use top-level await: `await (async function () { ... return value; })()`. All polling (autosave waits, element waits) requires this pattern. The action inside may have executed even when `{}` is returned. |
| Selector returns null at run-time | Page changed since the script was frozen | STOP and report which selector failed. Do not improvise a workaround. |
| CAPTCHA or challenge dialog appears | Platform anti-automation detection triggered | STOP immediately. Do NOT retry. Wait 30+ minutes. Check account for restrictions. |
| Field shows correct value but readback returns different/empty | React state divergence (visual state differs from app state) | Retry once with the alternative input technique. If still wrong after one retry: STOP and report the selector, intended value, and actual value. |
| Automation stalls / no progress | Browser tab moved to background (Chrome throttling) | Bring tab back to foreground. Chrome throttles timers in background tabs with no workaround. |
| Operating on wrong data / wrong account | Multiple Chrome profiles; automation running on wrong profile | Close automation. Verify correct Chrome profile in pre-flight. Restart. |
| Outlook domain guard aborts on valid Microsoft 365 session | Guard checks navigated URL; redirect landed on `outlook.cloud.microsoft` | Check the actual domain after the redirect settles. Both `outlook.office.com` and `outlook.cloud.microsoft` are valid Microsoft 365 destinations. |
| Outlook Conditional Access re-authentication prompt appears | Session expired or policy requires re-authentication | STOP. Ask the user to log in manually. Clicking a login submit button requires explicit per-session user permission. |
