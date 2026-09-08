# User Manual - Meeting Intel (Make.com + Microsoft 365)

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
4. Open `meeting_intel.json` in a text editor, select all, and copy the contents
5. Paste into the import dialog and click **Save**

The scenario will open with all 29 modules in place. You will see red warning icons on most modules - this is expected. The blueprint ships without any connections attached, so no module is live yet. You are not broken; you just need to connect your accounts.

---

## 2. Set up your four connections

Click each module that shows a red warning. In the module settings panel, look for the **Connection** field and click **Add** to create a new connection of the required type.

### Connection 1 - Microsoft Calendar (module 1)

Module 1 reads your Outlook 365 calendar using Microsoft's API. The connection type is **Microsoft 365 Calendar**.

- Click module 1, then **Add** next to the Connection field
- Make.com will open a Microsoft login prompt
- Sign in with the Microsoft account whose calendar you want to read
- Approve the permissions Make requests
- Name the connection something you will recognize (e.g. "My Outlook Calendar")

### Connection 2 - Apify (modules 6, 9, 13, 18, 28)

Apify runs the web research actors. All Apify modules share one connection.

- Click any Apify module (module 6 sits inside the first branch after the router; on the canvas it is the first Apify-branded module you see)
- Click **Add** next to the Connection field
- Enter your Apify API token (find it at apify.com/account/integrations)
- Save the connection

When you set the connection on one Apify module, Make will offer to use the same connection on the others. Accept each offer.

### Connection 3 - OpenAI (modules 2, 12, 17, 21, 23)

OpenAI powers the participant extraction, company analysis, and brief synthesis. All OpenAI modules share one connection.

- Click module 2, then **Add** next to the Connection field
- Enter your OpenAI API key
- Save the connection

When you set it on module 2, accept the same connection for modules 12, 17, 21, and 23.

### Connection 4 - Microsoft 365 Email (module 24)

Module 24 sends the finished brief via Outlook email. The connection type is **Microsoft 365 Email** - this is a separate connection type from the Calendar one above, even if it is the same Microsoft account.

- Click module 24, then **Add** next to the Connection field
- Sign in with the Microsoft account you want to send from
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
2. Find the **To recipients** field - it will show `{{email_to}}`
3. Replace `{{email_to}}` with the email address where you want to receive briefs
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

1. Make sure today's calendar has at least one meeting with at least one external attendee
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

**Changing the calendar window:** Module 1 fetches today's events using Microsoft's calendar API. To change the date range, click module 1 and edit the URL parameters. The default fetches events starting from the current date to the end of that calendar day.

**Adding a scheduled trigger:** If you want the brief to arrive automatically each morning, you can replace the on-demand setup with a scheduled trigger. Be aware that adding a polling trigger introduces a cursor initialization step that must be done via the module's right-click context menu in the Make editor - this is documented in the Troubleshooting section.

---

## 7. Troubleshooting

### No email arrives after a successful run

The brief only sends if research content is non-empty (filter "Has research content" on module 23) and the synthesized brief is non-empty (filter "Has brief content" on module 24). If all your meetings today were internal-only (all attendees share your domain), the participant extraction in module 2 returns an empty result, the filters block further processing, and no email sends. This is correct behavior, not a bug.

Check whether the filter on module 3 ("Has external participants") is passing. If you had meetings with external attendees and still got nothing, confirm that `{{user_domains}}` in module 2 is set correctly - if it is empty or wrong, all participants may be misclassified as internal.

### One email per meeting instead of one combined email

Module 26 (a TextAggregator) consolidates all per-meeting briefs before module 24 sends them. If you are receiving one email per meeting, module 24 has been moved or reconnected inside the per-event loop. Do not move modules 23 or 24 into the loop - they belong after module 26.

### The Make editor is showing old values after you made changes

The Make editor does not live-reload after changes pushed via its internal API. If you made changes in one browser tab and opened a second, or if you left the editor open while changes were saved, the tab may be showing stale data. Reload the page before editing or running. A stale tab that saves will overwrite the changes with the old values.

### OpenAI module returning a 400 error

Two common causes:

First, check that the `max_tokens` field in any OpenAI module is set to a number, not a string. Make accepts strings in the field; OpenAI rejects them at runtime with a 400.

Second, the synthesis module (module 23) uses gpt-5.6-luna. Some newer OpenAI models only accept `temperature: 1` - any other value returns a 400 with the message "Unsupported value". If you have changed the model in any OpenAI module, remove or reset the temperature field for that module.

### Filter operators not working as expected

Make's filter operators use specific names that differ from what you might expect. Valid operators include `text:notequal`, `number:greater`, and `number:less`. Variants like `text:notEqual` (capital E), `number:gte`, and `number:lt` are silently accepted by Make's configuration UI but do not execute correctly at runtime.

There is no "greater than or equal to" operator. To express "status code is 200 or above," the blueprints use `number:greater` with value `199`.

Do not replace the filter on module 3 with a `parseJSON()` expression. The `parseJSON()` function is available in mapper expressions but throws an error when used inside filter conditions. The shipped filter uses `text:contain "{"` to check whether the result contains at least one object - an empty JSON array `[]` contains no `{`, so this correctly identifies empty results.

### Expired Microsoft calendar connection

If module 1 fails with an error like "Cannot read properties of undefined (reading 'accessToken')" or a similar access token error, your Microsoft OAuth connection has expired. Make accepts the connection during setup without validating the token - the failure only appears at first run.

Fix: go to Make's Connections panel (left sidebar), find your Microsoft 365 Calendar connection, and click **Reauthorize**. Then re-run the scenario.

### LinkedIn search returns no results

Module 18 searches LinkedIn using the participant's name and company guess (extracted from their email domain). If the company guess is too specific, the search may return zero results. For example, searching "Name State University" may find nothing while "Name State" finds the person.

If you are seeing zero LinkedIn results for someone you know has a LinkedIn profile, the issue is likely the company guess value coming out of module 2. You cannot easily fix this per-run, but you can check what module 2 extracted by clicking on the module after a run and viewing the output bundle.

### LinkedIn actor returns empty or wrong results when you swap the actor

The LinkedIn search actor (module 18, actor ID: xfz4tG0OB6ClIGVGv) expects `keywords`, `mode`, and `maxResults` in its input. A different actor may use `searchQuery`, `firstName`, `lastName`, or other field names. Passing the wrong fields returns zero results with no error - the actor silently ignores unknown input. Before wiring in a different actor, test it in the Apify console with your intended input and confirm it returns profiles.

Also: the profile scraper (module 28, actor ID: LpVuK3Zozwuipa5bp) has a 120-second timeout. Some actors take longer - if you swap it for a different scraper, test it in the Apify console first to confirm it finishes within 120 seconds.

### Research payload causes module 21 to fail or produce nonsense output

Module 29 fetches LinkedIn profile data with a field whitelist (`firstName`, `lastName`, `headline`, `linkedinUrl`, `publicIdentifier`, `location`, `about`, `currentPosition`, `experience`, `education`, `connectionsCount`, `followerCount`, `topSkills`). This whitelist keeps the payload manageable for the validation model.

If you edit module 29 and remove or widen the `fields` parameter, full profiles with recommendations and certifications can reach module 21 with tens of thousands of tokens - enough to cause failures in candidate selection. Keep the whitelist. Also note that Apify's `fields` parameter does not support dot-notation into arrays (like `experience.position`) - list only top-level field names.

### Participant matching misses someone at a university or with a historical affiliation

Module 21 validates LinkedIn candidates against the prospect's name and company guess. The match is intentionally fuzzy: it checks the company guess against current position, past employers, education, and the about section. A person whose affiliation is a university appears in the education field and will still match.

If module 21 is saying "MATCH: none" for someone you found manually, check whether the company_guess extracted by module 2 is reasonable. A very generic guess ("Consulting") or a very specific one ("State University Department of Economics") may not align with how the person lists their affiliation on LinkedIn.

Do not tighten the matching logic in module 21's prompt to require current-employer-only - that would cause misses for anyone not currently at the guessed company.

### After import, all modules show connection warnings

This is expected and normal. The blueprint ships without any connections attached (`islinked: false` for all connection fields). This is intentional so that the file can be shared without exposing private account credentials. Work through Section 2 of this manual to attach your own connections.

### Make's scenario validator says it is valid but the run still fails

Make's blueprint validator checks structure, not behavior. It accepts string values where OpenAI requires numbers, unknown filter operators, and other issues that only surface at runtime. "Valid" in Make means the blueprint shape is acceptable, not that the run will succeed. Always do one live Run once test after any configuration change.

---

## 8. Privacy note

**Data sent to Apify:** The company email domain and company name guess for each external participant are sent to the Apify website crawler and Google Maps search actors. LinkedIn profile URLs are sent to the Apify profile scraper. Apify stores run inputs and outputs; check apify.com's privacy policy for retention details.

**Data sent to OpenAI:** The meeting title, meeting description preview, and attendee list from your Outlook calendar are sent to OpenAI in module 2 for participant extraction. Company website content and LinkedIn profile data are sent to OpenAI in modules 17, 21, and 23 for analysis and synthesis. OpenAI's data usage policies apply; if your organization has a data privacy agreement with OpenAI, that agreement covers this usage.

**Data accessed from Microsoft:** The scenario reads your calendar using the Microsoft Calendar OAuth connection. It reads event subject, body preview, start time, and attendee list. No data is written back to your calendar. Email is sent via the Microsoft Email connection using your Microsoft account.

**Local Make.com processing:** The blueprint, your connection credentials, and the module configurations live entirely within your Make.com account. No data is shared with anyone else via Make.com beyond what you configure.

---

## 9. Updating and uninstalling

**Updating:** If you receive an updated version of `meeting_intel.json`, import it as a new scenario rather than overwriting the current one. After confirming the new scenario runs correctly with your settings, delete the old one. Re-importing over an existing scenario does not preserve your connection assignments or setting values.

**Uninstalling:** To remove the scenario, go to Scenarios in Make.com, click the three-dot menu on the scenario, and select **Delete**. This removes the scenario and stops all future runs. Your connections (Microsoft, Apify, OpenAI) remain in Make's Connections panel and can be deleted separately from there if you no longer need them.

Your Apify account and OpenAI account are unaffected by deleting the Make scenario. If you want to fully remove the integration, also delete the relevant connections in those platforms' dashboards.
