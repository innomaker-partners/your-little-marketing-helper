---
name: platform-connector
description: Give Claude authenticated API access to a platform that sits behind OAuth, via a Make.com webhook proxy that holds the connection. Works for any API Make.com can authenticate to; Microsoft Graph and Meta are the worked examples.
---

# Platform API Connector

Give Claude authenticated access to an API that sits behind OAuth, without wiring that OAuth into Claude directly. Make.com holds the connection; Claude sends a structured request through one webhook, and Make.com routes it to the correct platform API. Any API Make.com can authenticate to works this way. Microsoft Graph (Calendar, Files, Mail) and Meta (Instagram, Facebook Pages) are the two worked examples this skill builds out end to end; "Adding a new provider" below is the general recipe for wiring any other.

## Operating rules

These hold across every step below - pre-flight, setup, and operations. Read them before you touch anything.

- **Set the scenario up through the Make MCP, not the browser.** Make's web UI is complicated and slow to drive, and clicking through it is where setup breaks. The Make MCP builds the scenario programmatically - create the webhook, create the scenario from the blueprint, activate it, and initiate the OAuth connection as a link the user clicks. It is a deferred tool set: load it with `ToolSearch` (`select:mcp__claude_ai_Make__scenarios_create,mcp__claude_ai_Make__hooks_create,mcp__claude_ai_Make__scenarios_activate,mcp__claude_ai_Make__credential-requests_create`) when you reach setup. The Make MCP is a one-time user prerequisite (it connects to the user's own Make account) - if it is not connected, tell the user to add it before continuing; see the Setup Guide. Use the browser (Claude-in-Chrome) only as a last resort - a step the MCP genuinely cannot do, or reading an error on a scenario you already created - and only when there is no MCP way. When you do, load those deferred tools then (`select:mcp__claude-in-chrome__tabs_context_mcp,mcp__claude-in-chrome__navigate,mcp__claude-in-chrome__read_page`) and use a fresh tab.
- **Connecting the OAuth account is the user's step, not yours.** The connection is authorized either by a consent link (from `credential-requests_create` on the MCP path) or by the "Create Connection" wizard (web-UI fallback). Either way: hand the link or the wizard to the user, ask them to grant access while you wait, then continue. It is the one part that must be the account owner signing in with their own login. Everything else - the webhook, the scenario, the share token, activation, the scripts - is yours. Never enter someone's credentials for them.
- **The OAuth connection is the only credential step; treat every other auth wall as a stop.** If you hit any other login or re-authentication prompt, STOP and tell the user - do not try to pass it. A CAPTCHA or security challenge is an immediate STOP: do not retry it and do not attempt to solve it.
- **Reads are safe to run; writes need explicit per-action consent.** Once the health check passes you may run GET/list calls. Anything that creates, edits, deletes, sends, or shares (POST/PUT/PATCH/DELETE) requires the user to approve that specific action first, and you confirm it against one real call before relying on it - a silent write to someone's live calendar or mailbox is not recoverable. If you cannot tell whether a call writes, treat it as a write and ask.
- **Redact secrets in anything you show.** When you print a request or response to explain a result, mask the webhook URL, the share token, and any OAuth token, cookie, or session string. `--verbose` already redacts the webhook id and token; keep them redacted in what you paste back.
- **Keep the fetched data local unless the user asks otherwise.** The data you pull is the user's own calendar, mail, or files. Render it locally by default - a file or an HTML page on disk. Do not upload it, publish it to an external URL or artifact, email it, or share it with any third-party service unless the user explicitly asks for that. Showing the user their own result is not the same as publishing it somewhere.
- **This plugin's own files are addressed from `${CLAUDE_PLUGIN_ROOT}`.** The connector script, the blueprint, and `REFERENCE.md` ship inside the plugin. `${CLAUDE_PLUGIN_ROOT}` is the absolute path Claude Code sets to this plugin's install location; prepend it whenever you read or run one of them (e.g. `${CLAUDE_PLUGIN_ROOT}/scripts/connector/platform_request.py`, `${CLAUDE_PLUGIN_ROOT}/REFERENCE.md`). The plugin directory is not the user's working directory, so a bare relative path will not resolve. The user's `.env` is the exception - it lives in the user's own project, not in the plugin.

## Pre-flight

Run these checks before any operation. If any fail, guide the user through the fix before proceeding.

### 1. Python 3

Run `python3 --version`. If missing, tell the user:

> "This skill needs Python 3.8+. Install from python.org (Windows: check 'Add to PATH' during install) or verify your system Python (macOS: pre-installed on most versions; Linux: usually pre-installed)."

### 2. Environment variables

The connector needs a `.env` file with two values: `MAKE_WEBHOOK_URL` and `MAKE_SHARE_TOKEN`. **These are produced by building the scenario, not supplied beforehand** - you invent the share token and set it in Make.com, and Make.com generates the webhook URL when the scenario is built. The `.env` is the last thing setup produces, not a prerequisite for it.

Check for the file in the user's project root (the directory you are working in). The `.env` belongs with the user's project, not inside the plugin - the plugin's install location is managed by Claude Code and is not a place to write credentials.

- **File present with both values non-empty:** the scenario was set up on a previous occasion. Skip the Setup Guide and go straight to the connection test (step 3).
- **File missing, or either value blank:** the scenario has most likely not been set up yet. Go to the **Setup Guide** below and build it - it ends by writing these two values into `.env`, after which you return here for the connection test. (If the user tells you the scenario already exists and only the file is missing, ask them for the webhook URL and the share token and write the `.env` yourself instead of rebuilding.)

Once the `.env` file is confirmed to exist, **load the values into the current shell session** before running any command:

```bash
if [ -f ".env" ]; then
  set -a; . ".env"; set +a
  echo "Loaded credentials from: .env"
else
  echo "ERROR: No .env file found in the project root"
fi
```

How to quote values in your `.env` depends on what characters the value contains:

- **Value contains `$` or a backtick**: use single quotes. Double quotes expand `$` and backticks, so the shell silently truncates anything that looks like a variable name or executes a substitution. The truncated token reaches Make.com, is rejected, and the error shows `share_token_mismatch`, which sends you to inspect Make.com settings that are actually fine. Single quotes preserve the value exactly:
  ```
  MAKE_SHARE_TOKEN='ab$cd&ef'
  ```
- **Value contains spaces, `!`, `&`, or similar characters but no `$` or backtick**: double quotes are sufficient:
  ```
  MAKE_SHARE_TOKEN="my secret token"
  ```
- **Value contains a literal single quote character**: single quotes cannot wrap it. Regenerate the token to get a value without a single quote.
- **Value is a plain alphanumeric string with no special characters**: no quotes needed.

Then verify that both values are non-empty:

```bash
[ -z "$MAKE_WEBHOOK_URL" ] && echo "ERROR: MAKE_WEBHOOK_URL is empty or unset" || echo "MAKE_WEBHOOK_URL: set"
[ -z "$MAKE_SHARE_TOKEN" ] && echo "ERROR: MAKE_SHARE_TOKEN is empty or unset" || echo "MAKE_SHARE_TOKEN: set"
```

If either value is blank after loading, say so explicitly:

> "Your `.env` file exists but `MAKE_WEBHOOK_URL` is empty. Please open `.env` and paste in the value you copied from Make.com."

**Do not proceed to the connection test or any operation until both values are confirmed non-empty.**

### 3. Connection test

Run:
```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/connector/platform_request.py --webhook-url "$MAKE_WEBHOOK_URL" --share-token "$MAKE_SHARE_TOKEN" --test
```

With your real token, all three checks must show PASS:
- `webhook_reachable: PASS` - the Make.com webhook URL responds
- `share_token_accepted: PASS` - the shared secret matches
- `scenario_active: PASS` - the Make.com scenario is turned on

If any show FAIL, consult the Error Reference in REFERENCE.md.

**Wrong-token test:** Then verify the token check is active. Run the same command with a deliberately wrong token:
```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/connector/platform_request.py --webhook-url "$MAKE_WEBHOOK_URL" --share-token "deliberately-wrong-token" --test
```

Three outcomes are possible:

- **`share_token_accepted: FAIL`**: this is the correct result. It means your scenario is rejecting tokens it does not recognise. Proceed to connect your OAuth accounts.
- **All three checks show PASS**: stop. Do not connect any OAuth accounts to this scenario. An all-PASS result on a token you know is wrong means every request is reaching your real platform connections without a token check, and anyone with the webhook URL could read your mail, calendar, and files. Open the scenario in Make.com, inspect the outer router filters on both routes, and do not proceed until this test produces `share_token_accepted: FAIL`.
- **Any other combination of FAIL results**: a setup problem exists independent of the token check. Consult the Error Reference in REFERENCE.md before connecting OAuth accounts.

## Setup Guide

Setup builds the Make.com scenario **through the Make MCP** (Option A) - it creates the webhook, the scenario, and the OAuth connection programmatically, so you avoid Make's web UI entirely. The web-UI paths (Options B and C) are fallbacks, used only when the Make MCP is unavailable or one specific step will not go through it.

The steps below set up Microsoft Graph and Meta as the two worked examples, because those are concrete enough to teach the whole flow. They are examples, not the tool's scope: any API Make.com can authenticate to installs the same way - wire it by following "Adding a new provider" below with that provider's own base URL and scopes.

**Prerequisite - the Make MCP.** Building via the MCP needs Make's official integration connected to Claude (it authenticates to the user's own Make account). When you reach setup, try to load the tools with `ToolSearch` `select:mcp__claude_ai_Make__scenarios_create,mcp__claude_ai_Make__hooks_create,mcp__claude_ai_Make__scenarios_activate,mcp__claude_ai_Make__credential-requests_create,mcp__claude_ai_Make__teams_list`. If they do not load, tell the user:

> "Setting up the connector uses Make's official integration. In Claude, connect the Make integration (Settings - Connectors), then tell me to continue."

This is a one-time user prerequisite, like having a Make account - it is not something this plugin ships, and it is not private to any one machine. It is needed only to *build* the scenario; once the scenario exists and `.env` is written, calling the connector never uses the MCP.

### Option A: Build via the Make MCP (recommended)

Work in one Make team; get its `teamId` from `teams_list` (or `users_me`) first. Every call below is scoped to that team.

1. **Create the webhook.** Call `hooks_create` with `typeName: "gateway-webhook"` and a name. Then `hooks_get` to read its URL (`https://hook.{region}.make.com/{id}`) - this is your `MAKE_WEBHOOK_URL`.
2. **Start the platform connection (the one human step).** Call `credential-requests_create` for the app you are connecting (`appName` e.g. Microsoft Graph, and the scopes/modules the operation needs - e.g. `Calendars.Read`). It returns a URL. **Give that URL to the user and ask them to open it and grant access** - that consent is the account owner's to give, and this is the only human step. It replaces clicking through Make's connection wizard. (The Azure AD app registration below still supplies the Client ID/secret behind that connection.)
3. **Create the scenario from the blueprint.** Read `${CLAUDE_PLUGIN_ROOT}/blueprints/universal_proxy.json`. Verify each module identifier in it via `app-modules_list` before creating (the API requires real module identifiers - never invent them). Put your chosen share token into the token-check filter (see note), reference the webhook from step 1 and the connection from step 2, and call `scenarios_create` with the blueprint `flow`, a `name`, `metadata: {"version": 1}`, the `teamId`, and `scheduling: {"type": "immediately"}`. It returns the new scenario ID; the scenario starts INACTIVE.
4. **Activate.** Call `scenarios_activate` with the scenario ID. (If it replies "Scenario is already running", treat that as success.)
5. **Save the two values and verify.** Write `.env` with `MAKE_WEBHOOK_URL` (step 1) and `MAKE_SHARE_TOKEN` (the token you set in step 3), quoting per Pre-flight step 2. Then run the Pre-flight step 3 connection test and the wrong-token test - that is what confirms the scenario works and rejects bad tokens.

**The share token via MCP.** The token check compares the incoming `share_token` to a fixed value. The clean way through `scenarios_create` is to put your chosen token as the literal comparison value directly in the token-check filter in the blueprint before you create the scenario, and use the same string in `.env`. (The web-UI paths below use a scenario variable named `SHARE_TOKEN` instead; both are valid, but a literal in the filter is what the API sets without extra steps.)

**Prefer a provider's native Make module.** If the provider has a dedicated Make app with a "Make an API Call" module (Microsoft Calendar -> `microsoft-calendar:makeApiCall`, and similar for other apps), prefer it over the blueprint's generic `http:MakeRequest` route. The native module accepts the connection that `credential-requests_create` produces directly, so you skip registering your own OAuth app and entering a Client ID/secret entirely - the whole build stays on the MCP path with no manual step. Verify the module name via `app-modules_list`, set its URL to the API path (the `endpoint` field), and reference the connection in its Credentials field. Use the generic `http:MakeRequest` route only where there is no native module for what you need (e.g. Microsoft Files/Mail may need the generic route even when Calendar does not). This is the single biggest ergonomic win: it is what lets the OAuth step be one consent click instead of an app registration.

**If a step will not go through the MCP** - the blueprint `flow` is rejected, a connection cannot be referenced, the token filter will not set - fall back to the web UI for that one step (Option B or C), then continue via the MCP. Do not force it, and do not silently switch the whole build to the browser. Report exactly what did not go through: it is a finding for this guide, not a failure to hide.

### Option B: Import the blueprint in the web UI (fallback)

Use this if the Make MCP is not connected, or an Option A step will not go through. This path drives Make's web UI - do it with the Claude-in-Chrome tools (deferred: `select:mcp__claude-in-chrome__tabs_context_mcp,mcp__claude-in-chrome__navigate,mcp__claude-in-chrome__read_page`; fresh tab first) or hand the steps to the user.

1. Log in to Make.com
2. Go to Scenarios → Import Blueprint
3. Upload the file `${CLAUDE_PLUGIN_ROOT}/blueprints/universal_proxy.json` from this plugin
4. **After import, the platform modules will show red "disconnected" badges.** This is expected. You need to connect your own OAuth apps. **This is the user's step (see Operating rules): pause, let the account owner do the Create Connection and consent, then continue.**
   - Click the **Microsoft Graph** module → Create Connection → enter your Azure AD app's Client ID and Client Secret → authenticate in the popup → grant the scopes listed below
   - Click the **Meta** module → Create Connection → enter your Meta App ID and App Secret → authenticate
   - You only need to connect the platforms you plan to use. Leave the other disconnected.
5. Set the share token: click the scenario settings (gear icon) → Variables → add a variable named `SHARE_TOKEN` with your chosen secret value (any string - you'll use the same value in your `.env`)
6. Activate the scenario: toggle the switch to ON
7. Copy the webhook URL: click the Webhooks module at the start of the scenario. The URL will look like `https://hook.{region}.make.com/{id}`
8. **Save the two values and verify.** You now have both: the share token you chose in step 5, and the webhook URL from step 7. Write them into a `.env` file in your project root:
   ```
   MAKE_WEBHOOK_URL=<the URL from step 7>
   MAKE_SHARE_TOKEN=<the token you chose in step 5>
   ```
   Quote the values per the rules in Pre-flight step 2 if they contain special characters. Then go back to Pre-flight step 3 and run the connection test and the wrong-token test - that is what confirms the scenario works and rejects bad tokens before you connect or call anything further.

### Option C: Manual build in the web UI (fallback)

If the Make MCP is unavailable and the blueprint import also fails (Make.com format changed, required modules not available on your plan):

The scenario has two nested routers: an outer router that separates valid tokens from invalid ones, and an inner router that routes to the correct platform. Build in the order shown: the token check and the platform dispatch are two separate routing layers, not filters on a single router.

1. Add a **Custom Webhook** trigger module to start the scenario.

2. Add a **Router** module directly after the webhook. This is the outer router. Give it two routes.

3. **Outer Route 1 - Token accepted**: Set a filter on this route: the `share_token` field from the webhook `equals (text)` the scenario variable `SHARE_TOKEN`. Inside this route, add a second **Router** module (the inner router) as its first and only module. Give the inner router three routes.

   a. **Inner Route 1 - Microsoft**: Set a filter on this route: the `platform` field from the webhook `equals (text)` `microsoft`. Add an **HTTP - Make a request** module with:
      - URL: `https://graph.microsoft.com/v1.0` followed immediately by the `endpoint` field from the webhook
      - Method: the `method` field from the webhook
      - OAuth connection: your Microsoft Graph connection (if this connection doesn't exist yet, click **Add** beside the field; Make.com will prompt for a Client ID and Client Secret - see **OAuth app registration → Microsoft** below for how to obtain them)
      - **"Return error if HTTP request fails": turn this OFF.** It is on by default. If left on, any platform 4xx or 5xx response aborts the scenario before the Webhook Response module runs and the caller receives nothing back.

      After the HTTP module, add a **Webhook Response** module. Set its `Status` to the HTTP module's **Status Code** output and its `Body` to the HTTP module's **Data** output. Do not hardcode status 200. A hardcoded 200 causes every platform error (403, 500, etc.) to be returned to the caller as a success, with the error payload silently embedded in the body.

   b. **Inner Route 2 - Meta**: Same structure as 3a, but filter `platform` equals `meta`, URL `https://graph.facebook.com/v26.0` followed by the `endpoint` field, and your Meta OAuth connection (if this connection doesn't exist yet, click **Add**; Make.com will prompt for a Meta App ID and App Secret - see **OAuth app registration → Meta** below).

   **Params forwarding (query and body).** You do **not** map params into the HTTP module, and you do **not** add a "Query parameters" array. The caller script (`platform_request.py`) pre-encodes them, so the module stays exactly as steps 3a/3b describe (only `url` and `method` mapped). For a **GET**, the script appends params to the endpoint as a URL-encoded query string; the module's URL field (`https://<base>` immediately followed by the `endpoint` field) forwards the whole URL untouched, so no query-parameter field is needed. Do not add one: the inner-pair key name differs between Make modules (`{name, value}` for the HTTP "Make a request" module, `{key, value}` for the Microsoft modules), so a hand-mapped array is a coin-flip that drops params silently with no error. For a **POST / PUT / PATCH** (writes), the script sends params as a JSON string in a `body` field; to enable writes, set the module's **Body content type** to `application/json`, **Body input method** to *JSON string*, and map **Body content** to the `body` field (`{{1.body}}`). Writes are gated: wire this only when the user has explicitly consented to a live write, and confirm it against one real call first. (Encoding note: a pre-encoded GET query passes cleanly through a provider's **native** Make module - the module forwards the URL as written, without re-encoding, so the filtered query reaches the platform intact. Prefer a native module where one exists. The generic `http:MakeRequest` route's inline-query handling is less predictable; if you must use it, confirm the query actually arrives before relying on it. The POST/body path follows the module schema; verify it against one real write before trusting it.)

   c. **Inner Route 3 - Health check**: Set a filter on this route: the `platform` field from the webhook `equals (text)` `test`. Add a **Webhook Response** module as the route's only module. Set `Status` to `200` (a literal integer, not a reference to an upstream module output) and `Body` to `{"status": "ok"}`. This route is the target of the `--test` health check in `scripts/connector/platform_request.py`. Without it, the test request matches no inner route, Make.com answers with the bare string "Accepted" before any Webhook Response module runs, and `--test` reports `scenario_active: FAIL` on a scenario that is working correctly. Note on the hardcoded 200: step 3a says not to hardcode status 200 on the platform routes, where a real platform error status must be forwarded to the caller. That rule does not apply here. This route has no upstream HTTP module and no platform response to pass through, so a literal 200 is the correct value.

4. **Outer Route 2 - Token rejected**: Set a filter on this route with the inverted condition: the `share_token` field from the webhook `does not equal (text)` the scenario variable `SHARE_TOKEN`. Add a **Webhook Response** module as the only module in this route. Set `Status` to `401` and `Body` to `{"error": "unauthorized", "detail": "share_token mismatch"}`.

5. **Scenario variable**: In scenario settings (gear icon) → Variables, add a variable named `SHARE_TOKEN` with your chosen secret value. Use this same value as `MAKE_SHARE_TOKEN` in your `.env` file.

6. **Activate the scenario**: toggle the switch to ON. Without this, the webhook accepts no requests.

7. **Copy the webhook URL**: click the Custom Webhook module at the start of the scenario. The URL will look like `https://hook.{region}.make.com/{id}`. This is the value that goes into `MAKE_WEBHOOK_URL` in your `.env` file.
8. **Save the two values and verify.** You now have both: the share token you set as the `SHARE_TOKEN` variable in step 5, and the webhook URL from step 7. Write them into a `.env` file in your project root:
   ```
   MAKE_WEBHOOK_URL=<the URL from step 7>
   MAKE_SHARE_TOKEN=<the same value you used for SHARE_TOKEN in step 5>
   ```
   Quote the values per the rules in Pre-flight step 2 if they contain special characters. Then go back to Pre-flight step 3 and run the connection test and the wrong-token test - that is what confirms the scenario works and rejects bad tokens before you connect or call anything further.

### OAuth app registration

#### Microsoft (Azure AD)

**You may not need this at all.** If you used the native `microsoft-calendar:makeApiCall` module (see "Prefer a provider's native Make module" in Option A), the `azure` connection from `credential-requests_create` already carries Make's own OAuth app, so there is no Client ID/secret to register - skip this section. Register your own Azure AD app only for the generic `http:MakeRequest` route, or when you need scopes or tenant control the native module does not give you.

Register an app at portal.azure.com → Azure Active Directory → App registrations → New registration:

1. **Name**: anything descriptive (e.g., "Claude Workflow Proxy")
2. **Redirect URIs**: select "Web" and register both of the following (Make.com uses both callback domains, and Azure AD requires an exact match):
   - `https://www.integromat.com/oauth/cb/microsoft-graph`
   - `https://www.make.com/oauth/cb/microsoft-graph`
   When Make.com's connection wizard opens during Step 4 above, it will show the exact URI it sends; confirm it matches one of the two above.
3. **API permissions**: click "Add a permission" → Microsoft Graph → Delegated permissions → add:
   - `Calendars.ReadWrite` - for calendar read/write operations
   - `Files.Read` - for OneDrive/SharePoint file operations
   - `Mail.Read` - for email read operations
   - Only add the scopes for the APIs you plan to use
4. **Admin consent**: In a managed organization (your company controls Azure AD), a tenant admin must click "Grant admin consent." If you are the admin, do it now. If not, send IT this information: "I need admin consent for a custom OAuth app that reads calendar, files, and mail via Make.com (SOC2 Type II certified). The scopes are Calendars.ReadWrite, Files.Read, and Mail.Read. Tokens are stored in Make.com, not on any local machine."

   If IT denies consent, there is no way for the plugin to bypass tenant policy. Options: (a) use a personal Microsoft account (outlook.com or a personal Microsoft 365 subscription, not subject to your organization's Azure AD), or (b) narrow the scope request to a single read-only permission (e.g., `Mail.Read` only). A single read-only scope is materially easier to approve than a three-scope request that includes `Calendars.ReadWrite`. A narrower grant limits which operations in REFERENCE.md are available, but those in scope work fully.
5. **Client secret**: Go to Certificates & secrets → New client secret → copy the value immediately (it's shown only once). You'll paste this into Make.com when creating the connection.

#### Meta (Instagram / Facebook Pages)

Register an app at developers.facebook.com → My Apps → Create App:

1. **App type**: select "Business"
2. **Add product**: Instagram Graph API
3. **Permissions needed**: `instagram_basic`, `instagram_manage_insights`, `pages_read_engagement`
4. **App Review**: These permissions require Meta's App Review. This takes **1-5+ business days** and requires:
   - A privacy policy URL (can be a simple page on your website)
   - A written explanation of how you'll use the data
   - A screenshare video demonstrating the use case
5. **Account requirements**:
   - Your Instagram account must be a **Business or Creator** account (personal accounts return nothing from insights endpoints)
   - The Instagram account must be **connected to a Facebook Page**
6. **Discovering your IDs** (needed for API calls):
   - After OAuth setup, go to developers.facebook.com → Graph API Explorer
   - Call `/me/accounts` → find your Page ID in the response
   - Call `/{page-id}?fields=instagram_business_account` → the `id` in the response is your `ig-user-id`
   - Save both IDs. You'll need them as parameters in API calls.

## Adding a new provider

Microsoft and Meta are worked examples. The proxy is provider-agnostic: any API that Make.com can authenticate to (OAuth 2.0, API key, or a token you pass through) can be added as one more route, in every case where routing through Make's auth is easier than calling that provider directly. Nothing in `platform_request.py` changes: it already sends `{platform, endpoint, method, body, share_token}` and pre-encodes GET params into the endpoint for you.

To add provider `X`:

1. **Scenario** - add one inner-router route (Option C, step 3, or the equivalent module in the blueprint you pass to `scenarios_create`), filtered `platform` `equals (text)` `X`:
   - An **HTTP - Make a request** module, URL `https://<X-base-url>` immediately followed by the `endpoint` field from the webhook (e.g. `https://api.hubapi.com/crm/v3` + `{{1.endpoint}}`). If X has a dedicated Make app with a "Make an API Call" module, that works too - its `url` is a relative path and it handles OAuth for you.
   - Method: the `method` field. **"Return error if HTTP request fails": OFF** (same reason as step 3a - otherwise platform errors abort before the response module).
   - Connection: X's OAuth or API key, defined in the module's **Credentials** field - never in a header or query parameter you map by hand.
   - Follow it with a **Webhook Response** module: `Status` = the HTTP module's **Status Code**, `Body` = its **Data**. Do not hardcode 200.
2. **Caller** - call the script with `--platform X --endpoint /... --method GET --params '{...}'`. GET params are pre-encoded into the URL automatically; you write nothing provider-specific. (Writes: see "Params forwarding (query and body)" above - gated.)
3. **Register the operations** - add X's endpoints, methods, required params, and OAuth scopes to the endpoint registry in REFERENCE.md, so calls are checked against a known-good list. An unregistered call still runs, but its scope and params have not been verified.
4. **Errors (optional)** - the taxonomy already maps 401/403/400/429 by HTTP status. To translate X's own in-body error codes for the 200-wrapped case (a Make module returning HTTP 200 with a platform error inside), add a `platform == "X"` branch to `_extract_embedded_status` in `platform_request.py`. Until then those surface as `http_200`; read the raw body with `--verbose`.

The bar is transfer, not recall: you should be able to wire a provider this skill never names using only the route shape above and the provider's own API docs.

## Operations

When the user asks to interact with a connected platform's API:

1. **Look up the operation** in the endpoint registry in REFERENCE.md. Find the matching operation, endpoint, method, required params, and OAuth scope. If the user's request does not match any registered operation, tell them it is outside the tested set, name the OAuth scope the call would require, and ask for explicit confirmation before sending. Unregistered calls are not blocked by the proxy, but the registry is what has been checked for correct scopes and parameters. A call with a missing scope surfaces as `missing_permission_scope`.
2. **Gather parameters** from the user. For example, for `calendar.events.list`, ask for the date range.
3. **Construct and run the request**:
   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/connector/platform_request.py \
     --webhook-url "$MAKE_WEBHOOK_URL" \
     --share-token "$MAKE_SHARE_TOKEN" \
     --platform {platform} \
     --endpoint "{endpoint}" \
     --method {method} \
     --params '{params_as_json}'
   ```
4. **Handle the response**:
   - If `"error": false`, parse `body` and present to the user in the format they need
   - If `"error": true`, check the `diagnosis` field against the Error Reference in REFERENCE.md and guide the user through the fix
   - If `"error": true` with `"body": "Accepted"` and `"diagnosis": "http_200"`, the request reached Make.com but no scenario route matched. Two things to check: (1) the `platform` value sent does not match any route filter: confirm it exactly matches one of your scenario's route filters (`microsoft` or `meta` for the worked examples, or whatever you named a provider you added); (2) the share-token filter in the scenario may not be matching: run `--test` to confirm the token is accepted and verify the `SHARE_TOKEN` variable in Make.com matches `MAKE_SHARE_TOKEN` in `.env`.
   - Use `--verbose` for debugging if the error is unclear

## Limitations

- **`/me/` endpoints return the signed-in user's data only.** For shared mailboxes (e.g., `marketing@company.com`), use `/users/{email}/messages` instead. This requires the `Mail.ReadWrite.Shared` scope. Tell the user to add it in Azure AD.
- **Make.com free tier**: 1,000 operations/month, 2 active scenarios maximum. Each proxy call uses at minimum 2 operations (webhook + API call). At ~15 calls/day you'll hit the monthly limit.
- **OAuth tokens expire.** If operations start failing with `oauth_token_expired`, the user needs to reauthorize the connection in Make.com (scenario → click the module → Connection → Reauthorize).
- **Enterprise environments**: IT may need to approve the Azure AD app (admin consent), whitelist Make.com webhook domains in the firewall, or allow the Chrome extension. The setup guide tells users what to communicate to IT, but the plugin cannot bypass these controls.
- **Meta Graph API version is pinned.** The scenario currently targets `v26.0`. Meta retires Graph API versions on a rolling schedule (roughly two years after release). If Meta calls start failing with version-related errors, update the URL in the Meta HTTP module: if you imported the blueprint, open `${CLAUDE_PLUGIN_ROOT}/blueprints/universal_proxy.json` in a text editor, find `graph.facebook.com/v26.0`, and replace the version; if you built manually, edit the URL directly in Make.com. The current supported versions are listed at developers.facebook.com/docs/graph-api/changelog.
