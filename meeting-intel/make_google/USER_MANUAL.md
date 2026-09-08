# User Manual - Meeting Intel (Make.com + Google Calendar)

---

## Contents

1. Import the blueprint
2. Set up your four connections
3. Configure your five settings
4. Your first run
5. How to read the brief
6. Routine use and customization
7. Troubleshooting
8. Privacy note
9. Updating and uninstalling

---

## 1. Import the blueprint

1. Log in to Make.com
2. In the left sidebar, click **Scenarios**, then **Create a new scenario**
3. Click the **three-dot menu** in the top right of the scenario editor and select **Import Blueprint**
4. Open `meeting_intel_google.json` in a text editor, select all, and copy the contents
5. Paste into the import dialog and click **Save**

The scenario will open with all modules in place. You will see red warning icons on most modules - this is expected. The blueprint ships without any connections attached, so no module is live yet. You are not broken; you just need to connect your accounts.

---

## 2. Set up your four connections

Click each module that shows a red warning. In the module settings panel, look for the **Connection** field and click **Add** to create a new connection of the required type.

---

### Before you connect anything - read this

The Google Calendar connection has a mandatory manual step that is easy to miss. If you skip it, the scenario runs without errors during setup but fails with a 403 on every actual run.

Why it happens: the "Make an API Call" module type declares an empty scope list. Make's Google connection type requests only basic profile information by default. Without the manual calendar scope, Google never shows a calendar consent checkbox during sign-in, and every calendar request returns 403 (Request had insufficient authentication scopes). Creating a fresh connection without the scope does not help - the scope must be added before you sign in.

The fix is part of the step-by-step procedure below. Follow it exactly.

---

### Connection 1 - Google Calendar (module 1)

Module 1 reads your Google Calendar using Google's Calendar API. Follow these steps in order:

1. Click **module 1**
2. Click **Add** next to the Connection field
3. In the connection dialog, click **Show advanced settings**
4. In the **Scopes** field, type or paste: `https://www.googleapis.com/auth/calendar`
5. Click **Sign in** (or **Continue to Google**) and complete the OAuth flow
6. When Google shows the consent screen, confirm that calendar access is listed and grant it
7. Name the connection something you will recognize (for example: "My Google Calendar")

If you did not see a calendar permission checkbox on the Google consent screen in step 6, stop. Delete the connection, go back to step 2, and make sure the scope is entered before clicking Sign in.

### Connection 2 - Apify (modules 6, 9, 13, 18, 28)

Apify runs the web research actors. All Apify modules share one connection.

- Click any Apify module (module 6 sits inside the first branch after the router; on the canvas it is the first Apify-branded module you see)
- Click **Add** next to the Connection field
- Enter your Apify API token (find it at apify.com/account/integrations)
- Save the connection

When you set the connection on one Apify module, Make will offer to use the same connection on the others. Accept each offer.

### Connection 3 - OpenAI (modules 2, 12, 17, 21, 23)

OpenAI powers the participant extraction, company analysis, and brief synthesis. All OpenAI modules share one connection.

- Click **module 2**, then **Add** next to the Connection field
- Enter your OpenAI API key
- Save the connection

When you set it on module 2, accept the same connection for modules 12, 17, 21, and 23.

### Connection 4 - Gmail (module 24)

Module 24 sends the finished brief via Gmail. The connection type is **Gmail** - this is a separate connection type from the Google Calendar connection above, even if it is the same Google account. The Gmail module requests its own email sending permissions automatically during the standard sign-in flow, so no manual scope step is needed here.

- Click **module 24**, then **Add** next to the Connection field
- Sign in with the Google account you want to send from
- This can be the same account as Connection 1, but you must create it as a separate connection in Make

After setting all four connections, the red warnings should be gone. If any remain, click that module and check whether a connection field is still unset.

---

## 3. Configure your five settings

Five values need to be set before you run the scenario. Three of them are Make template variables (you set them by editing a field value inside the module); two are plain text values hardcoded inside module content that you change manually.

### Setting 1 - Your company's email domains (module 2)

This tells the scenario which attendees are internal (and should be ignored) versus external (and should be researched).

1. Click **module 2**
2. In the system prompt field, find the line that reads: `the user's domain(s): {{user_domains}}`
3. The `{{user_domains}}` part is a Make template variable. Click on it or click the field to edit.
4. Replace `{{user_domains}}` with a comma-separated list of your company's email domains, for example: `acme.com,acme.io`
5. Save the module

If your company uses only one domain, just enter that one. Do not include the @ sign.

### Setting 2 - The email address to receive briefs (module 24)

1. Click **module 24**
2. Find the **To** field - it will show `{{email_to}}`
3. Replace `{{email_to}}` with the email address where you want to receive briefs, for example: `jane@acme.com`
4. Save the module

### Setting 3 - Your role and context (module 23)

This shapes what the brief emphasizes. One or two sentences about who you are and what you do.

1. Click **module 23**
2. In the system prompt field, find the line that reads: `User context: {{user_context}}`
3. Replace `{{user_context}}` with your description, for example: `I am a strategy consultant at Acme. I advise mid-market manufacturing companies on operational transformation.`
4. Save the module

### Setting 4 - Brief archetype (module 23)

The brief archetype sets the analytical frame the brief uses for conversation starters and emphasis. The default is `consulting`. The four options and what each produces:

- `sales` - fit assessment, pain points relative to your offering, buying signals, objection risks, deal openers
- `consulting` - engagement signals, likely challenges, budget indicators, decision-making structure, positioning angles
- `investor` - growth metrics, team assessment, market position, risk factors, due-diligence flags
- `general` - neutral intelligence with no product/service angle, suitable for networking or partnerships

To change it:

1. Click **module 23**
2. In the system prompt field, find the line that reads: `Brief archetype: consulting`
3. Replace `consulting` with whichever archetype fits your context
4. Save the module

### Setting 5 - Maximum participants to research (module 18)

The scenario researches up to 5 external participants per meeting by default. If a meeting has more than 5 external attendees, module 2's extraction step already prioritizes by seniority (CEO, CFO, VP, Director, Founder over generic roles). The cap for LinkedIn research is set separately in module 18.

To change the cap:

1. Click **module 18**
2. In the **Input JSON** field, find `"maxResults": 5`
3. Change `5` to your preferred number
4. Save the module

Note: higher values mean longer run times and higher Apify costs. The default of 5 is a practical balance.

---

## 4. Your first run

Before running, reload the Make.com scenario tab in your browser. The Make editor does not auto-refresh after configuration changes, and a stale tab can overwrite your settings.

Then:

1. Make sure today's Google Calendar has at least one meeting with at least one external attendee
2. Click **Run once** (the play button near the top of the scenario editor)
3. Watch the modules light up as the scenario executes - a typical run with one meeting and two participants takes two to four minutes
4. Check the email address you set in module 24

If the scenario completes but no email arrives, see the Troubleshooting section.

You do not need to click **Activate** to use on-demand runs. Activate only if you want the scenario to run on a scheduled trigger - which this blueprint does not include by default.

---

## 5. How to read the brief

The brief is an HTML email. Its structure follows this pattern:

**Header block**
- Meeting title and date
- Number of participants researched

**At a Glance section**
- One line per participant: name, role, company, and the single most useful fact the research found
- One sentence connecting the meeting to your context (from what you entered in Setting 3)

**Per-participant sections** (one for each person researched)
- Person subsection: current role, LinkedIn headline, professional background in two or three sentences, with confidence tags on each fact
- Company subsection: what the company does, size, location, markets, key products or services, industry position, with confidence tags
- Conversation Starters: three specific openers drawn from actual research findings, plus one or two that connect to your context from Setting 3

**Footer block**
- Research depth label: FULL, LIMITED, or MINIMAL
- Timestamp of when research was conducted
- Source URLs for click-through verification
- Confidence key

**Confidence tags** appear inline throughout the participant and company sections. Their meanings:

| Tag | Meaning |
|---|---|
| checkmark | Fact was verified directly on a source |
| ~ | Inferred from context (the inference basis is noted) |
| hourglass | Possibly stale - LinkedIn profiles are self-reported and not dated |
| ? | Information was not found in any source |

**Research depth levels**

| Label | What it means |
|---|---|
| FULL | Company website was crawled AND a matching LinkedIn profile was found and validated |
| LIMITED | One of those two sources was missing or inconclusive |
| MINIMAL | Research is based on web search snippets only - no crawled website, no validated LinkedIn |

When a participant's brief shows MINIMAL depth, treat the conversation starters with more caution - they are based on limited data.

The brief will always include every participant who was researched, even if nothing was found. If the scenario found nothing on someone, it will say so explicitly rather than omitting them.

---

## 6. Routine use and customization

**Running the scenario:** Click **Run once** whenever you want a brief for today. The calendar module fetches today's events at the moment you run it, so you can run it multiple times if meetings are added during the day.

**Changing the brief archetype:** Edit the `Brief archetype: consulting` line in module 23's system prompt (see Setting 4 above). You can change this for different meeting types - a different archetype produces a noticeably different emphasis in conversation starters and the company analysis.

**Changing the participant cap:** Edit `"maxResults": 5` in module 18's input JSON (see Setting 5). Keep in mind that researching more participants means longer runs and more Apify usage.

**Changing which calendar to read:** Module 1 fetches today's events from your primary Google Calendar using the URL `/v3/calendars/primary/events`. To read a different calendar instead, click module 1 and replace `primary` in the URL path with the calendar's ID. You can find a calendar ID in Google Calendar's settings by clicking the calendar name, then "Integrate calendar."

**Adding a scheduled trigger:** If you want the brief to arrive automatically each morning, you can replace the on-demand setup with a scheduled trigger. Be aware that adding a polling trigger introduces a cursor initialization step that must be done via the module's right-click context menu in the Make editor - this is documented in the Troubleshooting section.

---

## 7. Troubleshooting

### 403 error - Request had insufficient authentication scopes

**Symptom:** Module 1 fails during a run with a 403 error, typically mentioning "Request had insufficient authentication scopes" or "insufficient_scope."

**Why it happens:** The Google Calendar "Make an API Call" module type does not request any calendar permissions on its own. If the calendar scope was not manually added before signing in during connection setup, Google created the connection with only basic profile permissions. Every calendar API request then returns 403. Creating a new connection without fixing the scope does not help.

**Fix:**
1. Go to Make's Connections panel in the left sidebar
2. Find your Google Calendar connection, click its three-dot menu, and delete or disconnect it
3. Return to module 1 and click **Add** next to the Connection field
4. In the connection dialog, click **Show advanced settings**
5. Add the scope: `https://www.googleapis.com/auth/calendar`
6. Click Sign in and confirm that Google shows calendar access on the consent screen before you approve
7. Save and re-run the scenario

### Gmail connection returns "not found in options" or fails to send

**Symptom:** Module 24 fails, cannot find your Google connection in the dropdown, or reports a connection error even though your Google Calendar connection works fine.

**Why it happens:** Gmail and Google Calendar are separate connection types in Make. A Google Calendar connection cannot be assigned to a Gmail module, even if it is the same Google account. Module 24 requires a dedicated Gmail connection.

**Fix:** Click module 24, then **Add** next to the Connection field, and create a new Gmail connection by signing in with your Google account. Do not try to reuse or reassign the Calendar connection.

### No email arrives after a successful run

The brief only sends if research content is non-empty (filter "Has research content" on module 23) and the synthesized brief is non-empty (filter "Has brief content" on module 24). If all your meetings today were internal-only (all attendees share your domain), the participant extraction in module 2 returns an empty result, the filters block further processing, and no email sends. This is correct behavior, not a bug.

Check whether the filter on module 3 ("Has external participants") is passing. If you had meetings with external attendees and still got nothing, confirm that `{{user_domains}}` in module 2 is set correctly - if it is empty or wrong, all participants may be misclassified as internal.

### One email per meeting instead of one combined email

Module 26 (a TextAggregator) consolidates all per-meeting briefs before module 24 sends them. If you are receiving one email per meeting, module 24 has been moved or reconnected inside the per-event loop. Do not move modules 23 or 24 into the loop - they belong after module 26.

### The Make editor is showing old values after you made changes

The Make editor does not live-reload after changes. If you made changes in one browser tab and opened a second, or if you left the editor open while changes were saved, the tab may be showing stale data. Reload the page before editing or running. A stale tab that saves will overwrite the changes with the old values.

### OpenAI module returning a 400 error

Two common causes:

First, check that the `max_tokens` field in any OpenAI module is set to a number, not a string. Make accepts strings in the field; OpenAI rejects them at runtime with a 400.

Second, the synthesis module (module 23) uses gpt-5.6-luna. Some newer OpenAI models only accept `temperature: 1` - any other value returns a 400 with the message "Unsupported value." If you have changed the model in any OpenAI module, remove or reset the temperature field for that module.

### Filter operators not working as expected

Make's filter operators use specific names that differ from what you might expect. Valid operators include `text:notequal`, `number:greater`, and `number:less`. Variants like `text:notEqual` (capital E), `number:gte`, and `number:lt` are silently accepted by Make's configuration UI but do not execute correctly at runtime.

There is no "greater than or equal to" operator. To express "status code is 200 or above," use `number:greater` with value `199`.

Do not replace the filter on module 3 with a `parseJSON()` expression. The `parseJSON()` function is available in mapper expressions but throws an error when used inside filter conditions. The shipped filter uses `text:contain "{"` to check whether the result contains at least one object - an empty JSON array `[]` contains no `{`, so this correctly identifies empty results.

### Expired Google Calendar connection

If module 1 fails with an access token error (similar to "Cannot read properties of undefined (reading 'accessToken')") or a token expiry message, your Google OAuth connection may have expired or been revoked. Make accepts the connection during setup without fully validating the token - the failure only appears at run time.

Fix: go to Make's Connections panel in the left sidebar, find your Google Calendar connection, and click **Reauthorize**. Then re-run the scenario.

### Polling trigger cursor not initializing

If you add a scheduled trigger and the scenario runs but processes no events, you may need to initialize the trigger's start cursor. This cursor can only be set via a right-click on the trigger module circle in the Make editor - not inside the module settings panel. Right-click the module and look for "Choose where to start," then select "From now on." The shipped blueprint uses an on-demand API call rather than a polling trigger specifically to avoid this step.

### LinkedIn search returns no results

Module 18 searches LinkedIn using the participant's name and company guess (extracted from their email domain). If the company guess is too specific, the search may return zero results. For example, searching with a very long or specific company name may find nothing while a shorter version finds the person.

If you are seeing zero LinkedIn results for someone you know has a LinkedIn profile, the issue is likely the company guess value coming out of module 2. You cannot easily fix this per-run, but you can check what module 2 extracted by clicking on the module after a run and viewing the output bundle.

### LinkedIn actor returns empty or wrong results when you swap the actor

The LinkedIn search actor (module 18, actor ID: xfz4tG0OB6ClIGVGv) expects `keywords`, `mode`, and `maxResults` in its input. A different actor may use `searchQuery`, `firstName`, `lastName`, or other field names. Passing the wrong fields returns zero results with no error - the actor silently ignores unknown input. Before wiring in a different actor, test it in the Apify console with your intended input and confirm it returns profiles.

Also: the profile scraper (module 28, actor ID: LpVuK3Zozwuipa5bp) has a 120-second timeout. Some actors take longer - if you swap it for a different scraper, test it in the Apify console first to confirm it finishes within 120 seconds.

### Research payload causes module 21 to fail or produce nonsense output

Module 29 fetches LinkedIn profile data with a field whitelist (`firstName`, `lastName`, `headline`, `linkedinUrl`, `publicIdentifier`, `location`, `about`, `currentPosition`, `experience`, `education`, `connectionsCount`, `followerCount`, `topSkills`). This whitelist keeps the payload manageable for the validation model.

If you edit module 29 and remove or widen the `fields` parameter, full profiles with recommendations and certifications can reach module 21 with tens of thousands of tokens - enough to cause failures in candidate selection. Keep the whitelist. Also note that Apify's `fields` parameter does not support dot-notation into arrays (like `experience.position`) - list only top-level field names.

### Participant matching misses someone at a university or with a historical affiliation

Module 21 validates LinkedIn candidates against the prospect's name and company guess. The match is intentionally fuzzy: it checks the company guess against current position, past employers, education, and the about section. A person whose affiliation is a university appears in the education field and will still match.

If module 21 is saying "MATCH: none" for someone you found manually, check whether the company_guess extracted by module 2 is reasonable. A very generic guess ("Consulting") or a very specific one may not align with how the person lists their affiliation on LinkedIn.

Do not tighten the matching logic in module 21's prompt to require current-employer-only - that would cause misses for anyone not currently at the guessed company.

### After import, all modules show connection warnings

This is expected and normal. The blueprint ships without any connections attached. This is intentional so that the file can be shared without exposing private account credentials. Work through Section 2 of this manual to attach your own connections.

### Make's scenario validator says it is valid but the run still fails

Make's blueprint validator checks structure, not behavior. It accepts string values where OpenAI requires numbers, unknown filter operators, and other issues that only surface at runtime. "Valid" in Make means the blueprint shape is acceptable, not that the run will succeed. Always do one live Run once test after any configuration change.

---

## 8. Privacy note

**Data sent to Apify:** The company email domain and company name guess for each external participant are sent to Apify's website crawler and Google Maps search actors. LinkedIn profile URLs are sent to the Apify profile scraper. Apify stores run inputs and outputs; check apify.com's privacy policy for retention details.

**Data sent to OpenAI:** The meeting title, meeting description, and attendee list from your Google Calendar are sent to OpenAI in module 2 for participant extraction. Company website content and LinkedIn profile data are sent to OpenAI in modules 17, 21, and 23 for analysis and synthesis. OpenAI's data usage policies apply; if your organization has a data privacy agreement with OpenAI, that agreement covers this usage.

**Data accessed from Google:** The scenario reads your calendar using the Google Calendar OAuth connection (module 1). It reads event subject, description, start time, and attendee list. No data is written back to your calendar. Email is sent via the Gmail connection (module 24) using your Google account.

**Local Make.com processing:** The blueprint, your connection credentials, and the module configurations live entirely within your Make.com account. No data is shared with anyone else via Make.com beyond what you configure.

---

## 9. Updating and uninstalling

**Updating:** If you receive an updated version of `meeting_intel_google.json`, import it as a new scenario rather than overwriting the current one. After confirming the new scenario runs correctly with your settings, delete the old one. Re-importing over an existing scenario does not preserve your connection assignments or setting values.

**Uninstalling:** To remove the scenario, go to Scenarios in Make.com, click the three-dot menu on the scenario, and select **Delete**. This removes the scenario and stops all future runs. Your connections (Google Calendar, Gmail, Apify, OpenAI) remain in Make's Connections panel and can be deleted separately from there if you no longer need them.

Your Apify account and OpenAI account are unaffected by deleting the Make scenario. If you want to fully remove the integration, also delete the relevant connections in those platforms' dashboards.
