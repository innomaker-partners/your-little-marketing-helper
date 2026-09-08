# Meeting Intel -- Brief Template Reference

This document shows the structure that every Meeting Intel brief follows. It is a reference for understanding the output format -- not a file you fill in manually.

---

## The template

```
# Meeting Brief: [Meeting Title]
[Date] . [Time] . [Number of participants researched]

## At a Glance
- [One-line summary per participant: name, role, company, single most useful fact]
- If angled mode: one sentence connecting this meeting to the user's context

## [Participant Name] -- [Title/Role]

### Person
- Current role and tenure (if available)
- LinkedIn headline
- Professional background (2-3 sentences)
- Confidence: [which facts verified vs. inferred]

### Company: [Company Name]
- What they do (one sentence)
- Size, location, markets
- Key products/services
- Industry position / notable facts
- Confidence tags on each field

### Conversation Starters
- [3 specific, non-generic openers based on actual research findings]
- If angled mode: [1-2 openers connecting their world to the user's context]

## [Next Participant]
[Same structure]

---
Research depth: [FULL / LIMITED / MINIMAL]
Research conducted [timestamp]. Sources: [URLs actually visited].
Confidence key: ✓ verified on source . ~ inferred . ⏳ verified but possibly stale . ? not found
```

---

## Research depth indicators

Each brief closes with one of three depth labels, reflecting what was actually found during research:

- **FULL** -- company website crawled and LinkedIn profile found and validated. Both primary sources confirmed.
- **LIMITED** -- one source is missing. Either the company website was unreachable, or a LinkedIn profile was not found for this person.
- **MINIMAL** -- research is based on web search snippets only. No primary source was confirmed. Treat all facts in this section with additional skepticism.

---

## Confidence key

Facts in the brief carry inline confidence tags. They mean:

- **✓ verified on source** -- confirmed directly on the company's official website or another authoritative source
- **~ inferred** -- derived logically from available data, but not directly confirmed on any source
- **⏳ verified but possibly stale** -- sourced from a LinkedIn profile, which is self-reported and may not reflect the person's current situation
- **? not found** -- no source returned this information; the field is blank rather than guessed

When a section carries no tags, the research depth indicator for that participant applies to the section as a whole.

---

## Brief archetypes

The archetype is set in `config.json` during first-run setup. It shifts what the brief emphasizes in the Conversation Starters and the At a Glance summary. The underlying research is identical regardless of archetype.

**sales**
Emphasizes: product/service fit assessment, indicators of pain points or active buying signals, likely objections and how to address them, deal-opening angles. Conversation starters are oriented toward surfacing a problem the user can help solve.

**consulting**
Emphasizes: engagement signals, likely organizational challenges, indicators of budget or decision-making authority, who else in the organization may be involved in a buying decision. Conversation starters position the user as a peer thinking about the client's problem rather than a vendor pitching.

**investor**
Emphasizes: growth metrics and signals, team depth and track record, market position and competitive dynamics, risk factors and due-diligence flags. Conversation starters are oriented toward hypothesis validation rather than relationship building.

**general**
Neutral intelligence gathering with no product, service, or investment angle. Useful for networking meetings, partnerships, and introductory calls where the goal is understanding the other person rather than advancing a specific agenda. Conversation starters are curiosity-driven and open-ended.
