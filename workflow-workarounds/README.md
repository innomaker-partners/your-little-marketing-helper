> ### How to install these tools
> These are free tools for semi-technical marketers. You do not need to be a developer. There are two ways to set up anything in this repository:
>
> **1. If the tool is a Claude plugin** (all five tools have a plugin form): copy this repository's web address and paste it into the Claude app under Customize → Plugins → Add marketplace, then choose the tool and install it.
>
> **2. For any tool, or if you would rather not touch settings**: paste the repository's web address into a chat with Claude and ask it to set the tool up for you. Claude reads that tool's own instructions and walks you through it. The steps differ by tool (a Claude plugin, a Make.com scenario, and an n8n workflow are each set up differently), and Claude handles the difference.
>
> Repository address: `https://github.com/innomaker-partners/your-little-marketing-helper`

# Workflow Workarounds

Some platforms do not give Claude a clean way to do the work. Either there is no usable API at
all, or there is an API but it sits behind an OAuth wall that is painful to wire into an agent
directly. Workflow Workarounds is a Claude Code plugin with two independent skills, one for each of
those situations. Install it once and Claude gains both capabilities; use whichever the task in
front of you calls for.

---

## The two skills

**platform-connector, for a platform that HAS an API, behind OAuth.** Claude gets authenticated
access to that API without you wiring the OAuth into Claude. A Make.com scenario you own holds the
connection; Claude sends a structured request through one webhook, and Make routes it to the right
platform API and returns the response. Any API Make.com can authenticate to works this way.
Microsoft Graph (Calendar, Files, Mail) and Meta (Instagram, Facebook Pages) are the two worked
examples the skill builds out end to end, and it shows you how to add any other provider as one
more route.

**browser-automation, for a web workflow with NO usable API.** Claude develops a deterministic,
re-runnable browser script for the workflow, using the Claude-in-Chrome tools, and hands you the
script as the deliverable. The point is the artifact, not a one-off run: the script either exists
and re-runs, or the job is not done. LinkedIn analytics extraction and Outlook draft creation are
the two worked examples it teaches the method on. The method transfers to any page.

The two skills share nothing at runtime and can be used entirely independently. You do not need
Make.com to use browser-automation, and you do not need the Chrome extension to use
platform-connector.

---

## Which skill do I want?

- The platform has an API, but getting an agent authenticated to it is the hard part: **platform-connector**.
- The platform has no API worth using, and the work lives in the browser UI: **browser-automation**.
- Not sure: ask whether the data or action you need is reachable through a documented API. If yes,
  platform-connector. If the only way in is the website itself, browser-automation.

---

## Versions

| Folder | Platform | Full documentation |
|---|---|---|
| `claude_code/` | Claude Code plugin (both skills) | `claude_code/README.md`, `claude_code/USER_MANUAL.md` |

One version is published: the Claude Code plugin, which carries both skills. The `claude_code/`
folder is fully self-contained. A user who downloads only that folder has everything needed to
install and run the plugin: the manifest, both skills, the connector script and its Make.com
blueprint, the browser skill's reference material, setup instructions, and a troubleshooting guide.
There is no dependency on anything outside that folder.
