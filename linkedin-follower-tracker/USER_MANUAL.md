# LinkedIn Follower Tracker: User Manual

An n8n workflow that tracks how your LinkedIn following changes over time. On a
schedule it collects your current followers, works out who is new and who left,
enriches the new ones with company data, labels each one with a category you
define, appends everything to a Google Sheet, and emails you a summary.

Setup has two parts. **Part A** is what you build outside n8n (two PhantomBuster
phantoms and a Google Sheet). **Part B** is wiring it together inside n8n. Do
Part A first, because Part B needs the IDs and the sheet you create in Part A.

---

## 1. What it does, step by step

1. **Collect.** A PhantomBuster phantom pulls your current LinkedIn follower list.
2. **Diff.** The workflow compares that list against a master Google Sheet and
   splits it into *new followers* and *lost followers*.
3. **Enrich.** A second PhantomBuster phantom looks up each new follower and adds
   company and profile fields (industry, company size, headline, and so on).
4. **Classify.** Each new follower is sent to an AI step that returns exactly one
   category label. You define the categories (see section 4).
5. **Report.** Everything is appended to the master sheet, and you get an email
   with the totals: overall count, net change, lost count, and one line per
   category showing how many people carry that label.

You run everything on your own accounts, so there is no shared or per-user cost.
PhantomBuster is a paid product, so this tool suits people who already run, or
are ready to set up, a PhantomBuster LinkedIn follower collector.

---

## Part A. Set up outside n8n (do this first)

### A1. The two PhantomBuster phantoms

This workflow drives two specific PhantomBuster phantoms. Create both in your own
PhantomBuster account and follow each phantom's own tutorial for its setup
(connecting your LinkedIn session, and, for the collector, choosing whose
followers to collect). They need no settings beyond what their tutorials cover.

1. **LinkedIn Profile Follower Collector** (the "collect" step)
   Tutorial: https://phantombuster.com/automations/linkedin/3750/linkedin-profile-follower-collector/tutorial
   Role here: pulls your current follower list. Set it up per its tutorial,
   including connecting your LinkedIn account and pointing it at your own profile.

2. **LinkedIn Profile Scraper** (the "enrich" step)
   Tutorial: https://phantombuster.com/automations/linkedin/5589386912058181/linkedin-profile-scraper/tutorial
   Role here: takes the profile URLs of your new followers and returns their
   company and profile data. You do **not** configure its input by hand. The
   workflow hands it your sheet URL at launch, and it reads the `profileUrl`
   column from the run's tab. Just create it and connect your LinkedIn account.

**Two things to get right, because they are easy to miss:**

- **The agent ID.** Each phantom has a numeric ID, visible in its URL in your
  PhantomBuster dashboard. You will paste these two IDs into n8n in Part B. The
  collector's ID goes in the `Get an agent` node, the scraper's ID goes in the
  `Get an agent1` node.
- **The scraper reads your Google Sheet.** Because the enrichment phantom reads
  profile URLs out of your sheet, the sheet has to be reachable by PhantomBuster.
  Follow the phantom's tutorial for how it accesses a Google Sheet (typically the
  sheet shared so anyone with the link can view, or your Google account connected
  inside PhantomBuster). If the sheet is private and PhantomBuster cannot read it,
  the enrichment step will come back empty.

### A2. The Google Sheet

Create one Google Sheet to act as the database.

1. **Create the sheet**, and note its ID (the long string in the sheet URL between
   `/d/` and `/edit`). You will paste it into n8n in Part B.
2. **Create a tab named `Master`** (exact spelling). This is the running record of
   everyone, and it grows a little every run.
3. **Give the `Master` tab a header row.** Paste the following 43 column names
   into row 1 of `Master`, left to right, one per cell. This matters: the workflow
   appends to `Master` by matching these column names, so if the header row is
   missing or misspelled, the data lands in the wrong place or not at all.

   Tab-separated (paste into cell A1 and it fills across):

   ```
   classification	scrapeRound	fullName	firstName_from_input	lastName_from_input	profileUrl	followers	isFollowing	timestamp of download	companyIndustry	companyName	companyWebsite	linkedinCompanyUrl	linkedinCompanySlug	linkedinCompanyId	linkedinDescription	linkedinHeadline	linkedinIsHiringBadge	linkedinIsOpenToWorkBadge	linkedinJobDateRange	linkedinJobLocation	linkedinPreviousJobDescription	linkedinJobTitle	linkedinProfileId	linkedinProfileSlug	linkedinProfileUrl	linkedinProfileUrn	linkedinProfileImageUrl	linkedinSkillsLabel	location	mutualConnectionsUrl	connectionsUrl	linkedinCompanyName	linkedinCompanyDescription	linkedinCompanyTagline	linkedinCompanyFollowerCount	linkedinCompanyWebsite	linkedinCompanyEmployeesCount	linkedinCompanySize	linkedinCompanyIndustry	linkedinCompanyHeadquarter	linkedinCompanySpecialities	linkedinCompanyFounded
   ```

   This exact list is also defined in the workflow's `Build Header Item` node. If
   you ever change the columns, change them in both places or the two will drift.

You do **not** create the dated per-run tabs (named `Followers - YYYY-MM-DD`). The
workflow creates and fills those itself. You only create `Master`.

---

## Part B. Set up inside n8n

### B1. Import the workflow

In n8n: **Workflows → Import from File** and choose
`linkedin-follower-tracker.workflow.json`. The workflow imports inactive. Do not
turn it on until setup is finished.

### B2. Reconnect the four credentials

Every node that needs a login shows a credential named "Your ... account". Open
each one and select or create your own credential:

- **PhantomBuster** account (all PhantomBuster nodes)
- **Google Sheets** account (all Google Sheets nodes)
- **Gmail** account (the `Send a message` node)
- **OpenAI** account (the `AI Classification` node)

### B3. Set your two PhantomBuster agent IDs and LinkedIn cookie

Replace these placeholders:

| Placeholder | Where | What to put |
|---|---|---|
| `YOUR_FOLLOWER_COLLECTOR_AGENT_ID` | `Get an agent` | The ID of your Profile Follower Collector phantom (Part A1) |
| `YOUR_PROFILE_ENRICHMENT_AGENT_ID` | `Get an agent1` | The ID of your Profile Scraper phantom (Part A1) |
| `YOUR_LINKEDIN_SESSION_COOKIE` | `Add an agent to the launch queue` and `...queue1` | Your LinkedIn `li_at` session cookie |
| `YOUR_LINKEDIN_IDENTITY_ID` | `Add an agent to the launch queue1` | Your PhantomBuster identity ID |

You find a phantom's ID in its URL in the PhantomBuster dashboard. You find your
`li_at` cookie in your browser dev tools under Application, Cookies,
`www.linkedin.com`.

> **Security note.** The LinkedIn session cookie is a live login credential. It
> lets anyone who holds it act as your LinkedIn account. Keep this workflow
> private and do not export it with your real cookie in it.

### B4. Point the Google Sheets nodes at your sheet

Every Google Sheets node shows `YOUR_GOOGLE_SHEET_ID` as the document. Open each
one, and using the dropdown select the sheet you created in Part A2, then select
the correct tab:

- Nodes that read or write the running record use the **`Master`** tab.
- `Create New Tab`, `Append Header`, and the enrichment and classification nodes
  create and write the **dated tab per run**. You do not touch these.

### B5. Set the email recipient

In `Send a message`, change `your-email@example.com` to the address that should
receive the summary.

### B6. Set the schedule

Open `Schedule Trigger1` and choose how often it runs (for example weekly). Then
activate the workflow.

---

## 2. The first run

Run it once by hand (**Execute Workflow**) before trusting the schedule. On the
first run the master sheet is empty, so everyone is counted as a new follower.
That is expected. From the second run on, only real changes are reported.

If a run fails at a PhantomBuster step, check that the agent ID is correct, the
LinkedIn cookie is still valid, and the phantom is not already running. If the
enrichment columns come back blank, check that PhantomBuster can read your sheet
(Part A1).

---

## 3. Editing the classifier (the part you will want to change)

Out of the box, the `AI Classification` node ships with placeholder categories
(`Category A`, `Category B`, `Other`). You almost certainly want your own. You
can classify by anything: industry, seniority, company size, ICP fit, buyer
versus not, region, and so on.

Open the `AI Classification` node. In the **system message** you will see a block
marked clearly:

```
# ============================================================
# EDIT ZONE  --  change ONLY what is between these two markers.
# ...
# ============================================================
   ... your categories and rules ...
# ============================================================
# END EDIT ZONE
# ============================================================
```

### What you MAY change

Everything inside the EDIT ZONE:

1. **The category list.** Replace `Category A`, `Category B`, `Other` with your
   own labels. Keep them short and distinct. Always keep one catch-all label
   (like `Other` or `Not a fit`) for records that do not match.
2. **The decision rules.** Describe, in plain language, exactly what makes a
   record belong to each category. Be strict. Tell it to fall back to the
   catch-all when the data is thin or ambiguous.
3. **The examples.** Add a few worked examples of your own. Examples improve
   accuracy more than long rules do.

### What you MUST NOT change

**The OUTPUT CONTRACT** below the END marker. The workflow's next node,
`Extract Classifications`, expects the model to return a JSON object with a
single field, and it reads the first value out of it. If you change the output
format (for example, ask for a sentence, or several fields, or plain text), that
node will break and every follower will come back with a blank label.

So the rule is simple: **change the categories and the reasoning, never the shape
of the answer.**

### A worked example

Say you want to sort followers into `Decision maker`, `Practitioner`, and
`Other`. Your EDIT ZONE might read:

```
## Categories -- output exactly one of these labels:
- "Decision maker"
- "Practitioner"
- "Other"

## How to decide:
1. "Decision maker" if the job title clearly signals budget authority
   (Head of, Director, VP, C-level, Owner, Founder).
2. "Practitioner" if the title is an individual contributor or specialist
   with no clear authority (Manager without a team, Analyst, Coordinator,
   Specialist, Associate).
3. If the title is missing, unclear, or does not fit -> "Other".

## Examples:
### Example 1
linkedinJobTitle: *VP of Marketing*  ->  "Decision maker"
### Example 2
linkedinJobTitle: *Content Specialist*  ->  "Practitioner"
```

You leave the OUTPUT CONTRACT untouched. The model then returns something like
`{"classification": "Decision maker"}`, and the rest of the workflow just works.

The classifier is shown four fields per follower: `companyName`,
`companyIndustry`, `linkedinCompanyDescription`, and `linkedinJobTitle`. If your
categories need different fields, edit the **user message** in the same node to
pass them (any column from the sheet is available).

### Turning classification off

If you do not want AI labels at all, you can disable the `AI Classification`,
`Extract Classifications`, and `Save Classifications` nodes. New followers will
still be collected, enriched, and appended, and the email will still show the
new, lost, and net counts. The per-category lines will simply be absent.

---

## 4. Understanding the email report

The report counts labels **dynamically**. Whatever categories your classifier
produces, the email lists one line per category with its total across the whole
master sheet. You do not configure the categories anywhere except the classifier.
Change the classifier's labels and the report follows automatically.

A typical email looks like:

```
Overall connection number: 1240
Net increase: 18
Lost connections: 3
Total Decision maker: 402
Total Practitioner: 655
Total Other: 183
```

---

## 5. What this version does not include

This version does not cross-reference your followers against a separate outreach
target list. Doing that requires its own outreach-automation setup and is out of
scope for this tool. Everything about collecting, diffing, enriching, classifying,
and reporting followers is intact.
