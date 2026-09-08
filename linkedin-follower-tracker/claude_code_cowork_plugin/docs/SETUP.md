# Setup: linkedin-follower-tracker plugin

---

## Install

Install the plugin by pasting this repository's Git URL into Claude Code:
**Customize > Plugins > Add marketplace**, then paste the URL, and install
`linkedin-follower-tracker` from it.

Alternatively, you can hand the repository URL directly to Claude and ask it to
install the plugin.

Or upload the package directly in Claude Code: Settings > Plugins > Add.

After install, `${CLAUDE_PLUGIN_ROOT}` resolves to the plugin's installed directory.
The `claude` CLI on PATH is required for the ICP classification stage.

---

## Running in Cowork

In Cowork's sandbox, outbound network calls are blocked until you allow them. On
the first real run, approve **network egress** so the tool can reach:

- `api.phantombuster.com`, for every PhantomBuster launch, poll, and org-storage call
- the result-file URLs PhantomBuster returns for large collector and scraper outputs
  (signed download links on PhantomBuster's storage domain)

The free `--backend fake` rehearsal makes no outbound calls, so it runs without any
egress approval. Classification shells out to the local `claude` CLI, which must be
available inside the Cowork environment.

---

## PhantomBuster account setup

You need a PhantomBuster account and two specific phantoms:

### 1. LinkedIn Follower Collector

This phantom reads the follower list from a LinkedIn profile page. Search for
**"LinkedIn Follower Collector"** in the PhantomBuster store and launch it.
Configure it for the LinkedIn profile you want to track (paste the profile URL
into the phantom's "LinkedIn profile URL" field). Run it once manually to confirm
it returns a follower list before connecting it to this tool.

To find the agent ID: open the phantom in your PhantomBuster console and copy
the numeric ID from the URL (it looks like `/phantoms/1234567890`).

### 2. LinkedIn Profile Scraper

This phantom takes a list of LinkedIn profile URLs and returns company, job
title, and industry data for each. Search for **"LinkedIn Profile Scraper"** in
the PhantomBuster store and launch it. You do not need to pre-configure its
input list. The tool manages that via the PhantomBuster org-storage API.

To find the agent ID: same method as above.

### LinkedIn session

Your LinkedIn session cookie is **not** set up here. PhantomBuster stores it
inside each phantom's configuration (you paste your `li_at` cookie when you
first connect a phantom to your LinkedIn account). The tool fetches it live
from the phantom's stored config right before each launch, exactly as
PhantomBuster's own web interface does. You never paste it again once the
phantom is set up.

---

## Credential: the PhantomBuster API key

Log in to [console.phantombuster.com](https://console.phantombuster.com), go to
**Settings > API**, and copy the "X-Phantombuster-Key" value.

Store it in the `scripts/.env` file inside the plugin's skill directory:

```bash
cp ${CLAUDE_PLUGIN_ROOT}/skills/linkedin-follower-tracker/scripts/.env.example \
   ${CLAUDE_PLUGIN_ROOT}/skills/linkedin-follower-tracker/scripts/.env
# Open scripts/.env and fill in: PHANTOMBUSTER_API_KEY=your_key_here
set -a; source ${CLAUDE_PLUGIN_ROOT}/skills/linkedin-follower-tracker/scripts/.env; set +a
```

The `.env` file is gitignored and never committed. The `.env.example` template
is safe to commit because it holds only the variable name and a placeholder.

---

## Money gate

Every real PhantomBuster call requires `PB_LIVE=1` set in the environment. The
tool refuses to run `--backend real` without it. This makes every paid call
deliberate. You cannot accidentally spend credits.

Before any real run, rehearse with the fake backend (free, no credentials, same
pipeline mechanics):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/linkedin-follower-tracker/scripts/run_pipeline.py \
  --subject-dir <your-subject-dir> --stage all --backend fake
```

Only run with `--backend real` after confirming the fake run looks correct.

---

## Run-directory convention

Each LinkedIn profile you track gets its own directory, scaffolded by `--init`:

```
<parent-dir>/linkedin-follower-tracker/<slug>/
```

where `<slug>` is the lowercase hyphenated form of the subject label you give
during intake (e.g. `"Jane Doe"` becomes `jane-doe`). Each subject is fully
independent: its own master, its own phantom IDs, its own notification output.

Never put two subjects in the same directory. Never move or rename a subject
directory after the baseline is seeded. The master.csv and idempotency markers
live there by path convention.
