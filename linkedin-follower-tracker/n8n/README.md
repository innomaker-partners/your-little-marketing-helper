> ### How to install these tools
> These are free tools for semi-technical marketers. You do not need to be a developer. There are two ways to set up anything in this repository:
>
> **1. If the tool is a Claude plugin** (all five tools have a plugin form): copy this repository's web address and paste it into the Claude app under Customize → Plugins → Add marketplace, then choose the tool and install it.
>
> **2. For any tool, or if you would rather not touch settings**: paste the repository's web address into a chat with Claude and ask it to set the tool up for you. Claude reads that tool's own instructions and walks you through it. The steps differ by tool (a Claude plugin, a Make.com scenario, and an n8n workflow are each set up differently), and Claude handles the difference.
>
> Repository address: `https://github.com/innomaker-partners/your-little-marketing-helper`

# LinkedIn Follower Tracker - n8n version

This is the n8n version of the LinkedIn Follower Tracker. It is a single workflow file you import into your own n8n instance.

- **The workflow file:** [`linkedin-follower-tracker.workflow.json`](linkedin-follower-tracker.workflow.json)
- **Full setup and customization:** see the [USER_MANUAL.md](../USER_MANUAL.md) in the tool folder above.

To install: in n8n, choose Import from File and select `linkedin-follower-tracker.workflow.json`, then reconnect your own credentials as the manual describes. Or use install path 2 at the top of this page and let Claude walk you through it.

For the plugin version of this tool (Claude Code or Cowork), see the [tool README](../README.md).
