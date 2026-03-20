# ML Notes: Rule-First Signals, Shadow Models, and Forecasting

## Purpose
This note captures a practical ML direction for the Fabrication Flow Dashboard without weakening the existing explicit flow logic.

The current dashboard already produces meaningful operational conclusions from hard-coded rules. ML should therefore start as a companion to the rules, not as a replacement for them.

---

## Current Strength of the Dashboard
The dashboard is already doing real reasoning, especially in the traffic lights and attention panel.

### Traffic lights already encode flow judgment
Current top-strip signals are derived from explicit operational logic:
- `laser_buffer`
- `bend_buffer`
- `weld_feed_a`
- `weld_feed_b`

These are not cosmetic indicators. They already compress live fabrication state into actionable conclusions such as:
- healthy / watch / low / dry buffer conditions
- whether weld has meaningful near-term feed
- whether the next body is actually ready
- whether released work is present in the right upstream stages

### Attention panel already merges and de-duplicates high-value signals
The attention panel already acts as a focused decision surface by combining:
- dashboard attention items
- late release items
- behind-schedule rows

It also avoids duplicate explanation where a late release item already explains why something is behind.

### Release-hold logic is already schedule-aware
The current release-hold signal compares unreleased kits against:
- truck planned start date
- kit lag
- current date

This means one of the most important engineering-delay signals is already being computed directly rather than guessed.

---

## Main Conclusion
The dashboard already gives many of the right signals without ML.

That is not an argument against ML.
It is an argument for using ML in the right order.

The right first question is not:
- "Can ML replace the traffic lights?"

The right first question is:
- "Can ML learn to reproduce the current conclusions from raw dashboard state?"

If yes, that is valuable.
It means the tracked state is rich enough for learned models to recover the operational judgment currently encoded by hand.

---

## Recommended ML Strategy

## Phase 1: Shadow model only
Train ML models to predict the outputs of the current deterministic system.

Possible training targets:
- traffic light state for `laser_buffer`
- traffic light state for `bend_buffer`
- traffic light state for `weld_feed_a`
- traffic light state for `weld_feed_b`
- whether an attention item should appear
- attention tone / urgency bucket

This should run in parallel with the rules.

### Why this is useful
A shadow model creates a structured comparison layer:
- rule agrees with model
- rule disagrees with model
- model confidence is high or low

Those disagreements become extremely valuable review cases.
They may reveal:
- weak thresholds
- missing exceptions
- noisy inputs
- edge cases not yet encoded in rules
- areas where the rules are still better than learned behavior

### Important constraint
The shadow model should not drive production decisions.
At this stage it is for:
- validation
- comparison
- learning
- future design

---

## Phase 2: Forecast the existing signals
Once a shadow model can recover the current signal logic reasonably well, ML becomes more useful when it predicts the signals before they turn red or yellow.

Examples:
- probability bend buffer will become dry within one shift
- probability weld feed A will fall to low by tomorrow morning
- probability weld feed B will drop below threshold within the next release window
- probability a specific unreleased kit will become a critical late release item

This is where ML becomes more valuable than pure imitation.
The rules explain current state.
ML can help estimate near-future state.

---

## Phase 3: Rank engineering attention, not just current status
If forecasting works well, a later layer can rank unreleased or not-yet-completed engineering work by likely system value.

Examples:
- which unreleased kit is most likely to prevent a red signal
- which engineering release would relieve an upcoming dry buffer
- which release is likely to become parked WIP even if it looks urgent

This keeps the system aligned with real need:
- not "what should fabrication work on next?"
- but "what should engineering release next to protect flow?"

---

## Why Rule-First is Still the Right Foundation
The existing system has important advantages that should remain explicit:
- clear authority boundaries
- transparent thresholds
- explainable operational logic
- easy review when users disagree
- stable behavior under sparse data

ML should not replace those strengths.
It should sit beside them and extend them.

A good mental model is:
- rules define current operational truth
- ML learns the pattern of those truths
- ML later forecasts those truths before they happen

---

## Practical Targets for This Repo
The most natural starting targets are the surfaces that already summarize judgment well.

### Best first targets
1. Predict each traffic light state from raw dashboard state.
2. Predict whether a kit will appear in the attention panel.
3. Predict whether a currently unreleased kit will become a late-release attention item within a selected horizon.

### Best first output mode
Use ML as a hidden or developer-visible shadow layer first.
Do not replace the visible traffic lights or attention panel initially.

Possible early UX uses:
- log rule/model agreement rates
- show internal confidence during development
- list top disagreement cases for review

---

## Data This Approach Would Need
To support a useful shadow model and later forecasting layer, the system should preserve or log:
- raw truck and kit state snapshots over time
- release state transitions
- stage transitions
- planned start dates
- kit lag standards
- generated dashboard metric states
- generated attention lines or underlying attention items
- timestamps for when signals changed state

The most important thing is not fancy model choice.
It is preserving enough time-based state to learn from real sequence and flow behavior.

---

## Non-Goals
This note does **not** recommend:
- replacing deterministic rules with a black-box scheduler
- removing explainable thresholds from the current dashboard
- using ML to directly control the board
- treating model agreement as proof that the model is better

---

## Recommended Next Step
Add lightweight logging that records:
- raw state inputs
- computed dashboard signals
- computed attention outputs
- timestamp

Once that exists, build a shadow model that predicts the current lights and attention outcomes from raw state.

That will answer the key question cleanly:

> Can ML reach the same operational conclusions as the current hard-coded logic?

If yes, the next step is forecasting those same conclusions before they happen.
