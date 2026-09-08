# E1 - Pain-Point & Language Mining (gym / Test City)

> **Deterministic inputs:** `synthesis_facts.json` → `pain_points_low_star`. But the word-list is only a floor - **read the low-star reviews** and pull the real language. Claude does the clustering and judgment.
>
> **What a strong report contains (aim for a rich language bank, on the order of 100+ quotes when the corpus supports it):** build a **tiered pain taxonomy** (Tier 1 deal-breakers / Tier 2 serious / Tier 3 friction), each pain with its frequency and emotional intensity; a **verbatim language bank** grouped by pain theme (as many real quotes as the corpus supports - this is the raw material for copy); a **pain-to-promise map** (each pain → the counter-promise the client makes); and an **ad-headline bank** built from the pains. Note the review-count denominator honestly (small low-star N = directional, not statistical).

> **Template note:** This is a worked example on sample data showing the required structure and depth. In the real report, wherever a customer's words belong, reference the real quote by its ID as instructed in the task rules. Never type a customer quote in quotation marks and never copy the sample wording from this template.
>
> **Multi-source note:** This worked example runs on a single Google Maps corpus, but a real run can combine several review sources (maps, trustpilot, appstore, googleplay, reddit, g2, capterra). When the corpus spans more than one source, the header must disclose N per source (from the `per_source` and `source_distribution` keys in the meta block) and every quote is attributed to its source. Any source with no real star ratings, such as reddit, has its pain/praise split inferred from language rather than a rating, so it must be flagged as directional wherever it appears.

## Header block

Generated: 2026-08-27. Source: gym / Test City low-star reviews, Google Maps (<=3 stars with text = 7 of 15 total; star distribution: 1 star x2, 2 star x5, 4 star x3, 5 star x5). _Single-source example; a multi-source run discloses N per source here. Small N: directional signal only, not statistically representative._

---

## Executive summary

- **The #1 pain is equipment that stays broken.** The phrase "broken" appears in 3 of 7 low-star reviews and in zero high-star reviews (distinctiveness score: 42.86). "Machines" follows the same pattern. This is not a topic that comes up across all reviews - it is a uniquely low-star complaint.
- **The deal-breaker cluster is maintenance failure combined with overcrowding.** Reviewers describe broken equipment as a permanent state ("always", "constantly", "everywhere") and combine it with long waits for working machines. The emotional charge is chronic frustration, not one bad visit.
- **The single biggest opportunity is positioning on equipment reliability.** No competitor who earns this complaint can credibly claim it. A gym that can demonstrate working, maintained equipment answers the top dissatisfier with a provable, specific promise.
- **Facility hygiene is the second cluster.** Two reviewers use visceral language ("disgusting", "smell terrible", "dirty") for locker rooms and general cleanliness. This is emotionally intense even at low frequency.
- **Staff behaviour is a serious complaint but also a positive signal in high-star reviews.** The word "staff" appears in 2 low-star and 4 high-star reviews, meaning good staff is a genuine differentiator in both directions.
- **Client risk to flag:** All three Tier 1 and Tier 2 promises (equipment maintenance, cleanliness, staff presence) depend entirely on the client's actual operations. Do not lead with any of these without confirming the client's process is demonstrably better than competitors.

---

## 1. Pain-point taxonomy (tiered)

### Tier 1 - Deal-breakers (highest frequency + intensity)

**1a. Equipment out of order, never repaired**
The most distinctive low-star signal in this corpus. Reviewers describe broken equipment not as occasional but as a permanent, unfixed condition. The word "broken" appears in 3 of 7 low-star reviews with zero high-star appearances. The word "machines" (2 low-star, 0 high-star) reinforces the same theme. Emotional intensity: high. The word choice - "always", "constantly", "everywhere" - signals chronic failure, not an isolated incident. One reviewer draws the explicit conclusion: waste of money.

**1b. Machine scarcity due to broken stock and overcrowding**
Combines with 1a: when equipment breaks down and is not repaired, the remaining working machines become scarce. Reviewers cite both broken machines and long waits as a linked problem. Overcrowding amplifies the effect. Emotional intensity: medium-high (frustration, futility).

### Tier 2 - Serious complaints

**2a. Facility hygiene: locker rooms and general cleanliness**
Two reviewers use strongly negative sensory language for locker rooms ("disgusting", "smell terrible", "no cleaning at all") and general cleanliness ("the place is dirty"). Low absolute count in this corpus, but the language is emotionally intense and the kind of detail people share with others. Emotional intensity: high.

**2b. Staff availability and attitude**
Two reviewers cite staff problems: one describes staff as unavailable (absent when members need assistance), the other as rude. Note that staff is also mentioned positively in high-star reviews, making this a genuine differentiator - good staff earns loyalty, bad staff earns a 1-star review. Emotional intensity: medium-high.

### Tier 3 - Friction points

**3a. Cancellation process**
One reviewer calls the cancellation process "awful." Single mention in this corpus, but cancellation friction is a well-known gym industry pain point and a recurring theme in larger corpora. Treat as a flag to monitor as the dataset grows.

**3b. Value-for-money perception**
Two reviewers make explicit value judgements ("Waste of money", language naming the fee as unjustified). This is a downstream consequence of Tier 1 and Tier 2 failures rather than a standalone pain - when the equipment does not work and the facility is not clean, the membership fee feels unjustified. Fixing the upstream pains removes this complaint.

---

## 2. Verbatim language bank

Build the fullest language bank the corpus supports. Reference every distinct low-star quote by its ID, grouped by the pain theme it belongs to, one reference per line. This bank is the raw material for the client's copy, so completeness matters: if the corpus has forty relevant low-star quotes, reference forty, not four. Do not cap the bank at the number of themes or at any sample count.

### Theme A: Broken and unrepaired equipment

_[Reference every low-star quote by its ID that belongs to this theme, however many there are, one per line. This theme covers reviews describing broken equipment as a permanent, unfixed condition present throughout the facility.]_

**Copy notes:** The language is permanent rather than occasional, naming the failure as an unfixed chronic state. "Always" and "constantly" signal duration; "everywhere" signals scale. The financial injury is named directly: the fee feels unjustified when the equipment does not work.

### Theme B: Facility hygiene

_[Reference every low-star quote by its ID that belongs to this theme, however many there are, one per line. This theme covers reviews using visceral sensory language for locker rooms and general cleanliness.]_

**Copy notes:** "Disgusting", "smell terrible", "no cleaning at all" are visceral and sensory. "Dirty" appears as a summary word for general facility condition. These are the words prospective members use when warning friends.

### Theme C: Staff availability and attitude

_[Reference every low-star quote by its ID that belongs to this theme, however many there are, one per line. This theme covers reviews citing staff absence when members need assistance, or staff rudeness.]_

**Copy notes:** "Never available", "rude", "poor customer service" - the language covers both presence (they are not there) and attitude (when they are, it does not help). The contrast with high-star reviews - where staff is presumably a positive - makes this the most copyable differentiator.

---

## 3. Pain-to-promise map

| Pain (theme) | The promise that answers it |
|---|---|
| Equipment broken and never repaired | Every piece of equipment inspected and operational. Broken equipment flagged and repaired same day. |
| Equipment constantly out of order, broken machines everywhere | Working machines across the full floor - not half a rack of broken equipment and a waiting list. |
| Long waits for machines | Enough working equipment at peak times so you can train without waiting. |
| Locker rooms filthy, no cleaning maintained | Locker rooms cleaned on a schedule you can see - not cleaned once and forgotten. |
| General facility cleanliness poor | A clean facility maintained throughout the day, not just before opening. |
| Staff absent when members need help, poor service | Staff present on the floor during every staffed hour - not behind a desk or in back. |
| Staff rude to members | Staff who treat members as adults who know why they are here. |
| Cancellation process painful and obstructive | Cancel by phone or online in under two minutes. No runaround. |

---

## 4. Ad-headline / copy formulas

### Equipment reliability (answers Theme A)

- Finally, a gym where the equipment actually works.
- Every machine operational. Every session. Or we fix it today.
- We track maintenance so you can track your lifts.
- Not 'some machines working.' All of them.
- Broken equipment is the #1 reason people quit gyms. It's not a reason here.
- The machine you need will be working when you get here.
- We replaced the excuse 'that one's broken' with a maintenance schedule.

### Cleanliness (answers Theme B)

- Locker rooms you would let your kids use.
- Clean the day you joined. Clean six months later.
- A facility that gets cleaned between your arrival and your workout.
- We cleaned before you got here. And we will clean after.

### Staff presence (answers Theme C)

- Staff who are on the floor, not on their phone.
- Someone available when you need a question answered.
- Our staff are here when you need them.
- We hired people who want to help - and then we put them where you can find them.

### Value-for-money (answers Theme B3)

- A membership that earns itself every session.
- Paying for equipment that works. That is what a membership fee should buy.
- No more paying for broken machines and dirty floors.

---

## 5. Actionable takeaways

**Priority 1: Lead with equipment reliability - it is the clearest, most distinctive dissatisfier.**
"Broken" and "machines" score highest for distinctiveness among low-star phrases, with zero high-star appearances. The emotional language ("always", "constantly", "everywhere", "nobody fixes it") signals a chronic experience that has ended memberships and triggered explicit financial regret. A claim that every machine is maintained and operational directly names the wound. This is the strongest available hook.
**Client dependency to flag:** Before building a campaign around equipment reliability, verify that the client's maintenance process is genuinely and demonstrably better. If the client's own equipment breaks and sits unrepaired, this promise is a trap. Confirm the operations story before you write the copy.

**Priority 2: Cleanliness as visible, provable proof.**
"Disgusting" and "smell terrible" are among the highest-intensity words in this corpus. Cleanliness is photographable, schedulable, and checkable on a first visit. A prospective member can verify this promise before they sign. It is also a category where a clean gym wins without the competitor needing to do anything wrong - the contrast is obvious in person.
**Client dependency to flag:** The claim only holds if cleaning schedules are real, visible, and enforced. "No cleaning at all" in a competitor review suggests the bar is low - but only clear it if the client is actually clearing it.

**Priority 3: Staff presence as a differentiator (with a caution on overpromising).**
Staff appears in both low-star and high-star reviews, meaning it cuts both ways. Good staff is a genuine positive; absent or rude staff earns the lowest ratings (two 1-star reviews in this corpus cite staff problems). The message is presence and helpfulness, not personality claims.
**Client dependency to flag:** If the client's staffed hours are limited, do not promise staff availability across all hours. The promise should match the actual coverage or it generates the same complaint you are trying to answer.

---

_Note: All findings above are directional. The low-star sample is N=7 of 15 total reviews. No finding should be treated as statistically representative. As the dataset grows, track whether the equipment and cleanliness clusters hold at higher N - the distinctiveness scores suggest they will, but confirmation requires more data._
