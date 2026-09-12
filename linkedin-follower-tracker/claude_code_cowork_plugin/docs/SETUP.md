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

## Running in Cowork: allow the domains this tool uses (one-time)

If you run this tool inside Cowork, the first run can fail before PhantomBuster is
ever called, with a network or connection error and **nothing charged**. This is not
a bug in the tool. Cowork sandboxes block outbound network by default. The fix is
to allow egress for a small, specific set of domains before any real run.

1. Open **Settings > Capabilities** in Cowork.
2. Turn on **Allow network egress**.
3. Add the domains listed below. Do **not** use "All domains".
4. Run the pre-flight probe below to confirm the connection is open.
5. Then proceed with setup.

**The domains to allow.** PhantomBuster runs your phantoms on its own servers, so
the sandbox itself never touches LinkedIn directly. What the sandbox reaches on its
own is a small set:

- **`api.phantombuster.com`** (certain): every PhantomBuster call goes here,
  including launching phantoms, polling for results, fetching output, and all
  org-storage list and lead operations. This is the one entry you can be confident
  of on any run.
- **`api.anthropic.com`** (allow if the classifier CLI needs egress in your
  environment): the ICP classification step shells out to the `claude` CLI. In some
  Cowork environments that CLI makes outbound calls to reach the model; in others it
  is satisfied locally. Add this domain if classification fails with a network error
  after the PhantomBuster stages succeed.
- **PhantomBuster's result-storage host** (confirm on your first large run): when a
  collector or scraper run returns many rows, PhantomBuster returns a signed URL to a
  file on its result-storage service rather than returning the data inline. The
  sandbox fetches that URL directly. The exact hostname appears in the URL the tool
  prints at the fetch step. Allow it if that step fails with a connection error. Do
  not guess the hostname; wait until you see it in a run.

**Why not "All domains".** These phantoms run entirely on your PhantomBuster account
and nothing flows back except the follower and job-title data you asked for. "All
domains" gives no benefit but is needlessly broad. The set above is everything the
sandbox actually reaches; allowing exactly it keeps your egress tight.

---

### Pre-flight probe

Run this once in the Cowork sandbox before any real run. It checks that the sandbox
can reach PhantomBuster. It uses no credentials, costs nothing, and takes under five
seconds. A `401` response is the expected success signal: PhantomBuster rejected the
credentialless request, which means the connection itself is open. Any connection
error means egress is still blocked.

```bash
python3 -c "
import urllib.request, urllib.error, sys
try:
    urllib.request.urlopen('https://api.phantombuster.com/api/v2/agents/fetch-output', timeout=5)
    print('Reachable.')
except urllib.error.HTTPError as e:
    # ANY HTTP status means the connection got through and PhantomBuster answered
    # (a keyless request is expected to be rejected, typically 401). Egress is open.
    print('Reachable (PhantomBuster answered with HTTP', str(e.code) + '); egress is open.')
except Exception as e:
    print('Network blocked:', e)
    print('Fix: Settings > Capabilities > Allow network egress, then add api.phantombuster.com')
    sys.exit(1)
"
```

If the probe exits cleanly, the sandbox is ready for a real run. If it prints
"Network blocked", re-check the egress settings and re-run the probe before touching
the live pipeline.

The free `--backend fake` rehearsal makes no outbound calls and runs with no egress
setup at all. Run it first even once egress is confirmed.

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
