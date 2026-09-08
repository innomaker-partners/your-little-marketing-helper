> ### How to install these tools
> These are free tools for semi-technical marketers. You do not need to be a developer. There are two ways to set up anything in this repository:
>
> **1. If the tool is a Claude plugin** (all five tools have a plugin form): copy this repository's web address and paste it into the Claude app under Customize → Plugins → Add marketplace, then choose the tool and install it.
>
> **2. For any tool, or if you would rather not touch settings**: paste the repository's web address into a chat with Claude and ask it to set the tool up for you. Claude reads that tool's own instructions and walks you through it. The steps differ by tool (a Claude plugin, a Make.com scenario, and an n8n workflow are each set up differently), and Claude handles the difference.
>
> Repository address: `https://github.com/innomaker-partners/your-little-marketing-helper`

# Content at Scale

Producing a lot of content at once usually forces a trade: either you write each piece by hand and
keep the quality, or you generate a batch fast and spend the saved time catching the ways it went
wrong. The pieces drift from what your company actually does, invent a statistic nobody can source,
repeat each other, and read like a model wrote them.

Content at Scale is a Claude Code plugin that produces many pieces in one run and puts the quality
control inside the run, where you can check it, rather than leaving it for you to find afterwards.
It works with you: you supply the ground truth and make the judgment calls the tool cannot, and it
does the production and the mechanical checking, and it stops and asks you when something genuinely
needs a person.

---

## What it does

You give it your raw material (research documents, meeting transcripts, keyword lists), tell it what
you want written and in whose voice, and it runs a pipeline that:

- **Records your ground truth first.** Before anything is written, it interviews you for what your
  company and product actually are, whose voice the pieces should carry, and what you expect. That
  record is what every piece is checked against.
- **Labels where every fact came from.** A claim you asserted, a claim from your source material, a
  claim the tool found online, and a claim the model merely inferred are tracked as four different
  things. The ones the pipeline produced itself are the ones it challenges hardest.
- **Runs deterministic gates on each piece.** Length, verbatim overlap against your sources,
  fact-checking, AI-tell removal, and a before-and-after claim comparison. These are plain checks
  with numbers you can read, not a second model asked for its opinion.
- **Reviews the whole run against itself.** After the pieces are written it looks for two pieces
  that repeat each other, and for any piece that contradicts another or drifts off your brand.
- **Stops for you when it must.** A contradiction in your ground truth, a claim it cannot source, a
  sentence it cannot safely rewrite: these go to you, not around you.

Nothing it delivers carries an invented figure, and the file it hands you is provably the same file
its checks measured.

---

## Versions

| Folder | Platform | Full documentation |
|---|---|---|
| `claude_code/` | Claude Code plugin | `claude_code/README.md`, `claude_code/USER_MANUAL.md` |

One version is published: the Claude Code plugin. The `claude_code/` folder is fully self-contained.
A user who downloads only that folder has everything needed to install and run the plugin: the
manifest, every skill, the pipeline scripts, the bundled voice references, and the reference
document the skills read at runtime. It uses only the Python standard library, needs no API keys or
external accounts, and behaves identically on any machine. There is no dependency on anything
outside that folder.
