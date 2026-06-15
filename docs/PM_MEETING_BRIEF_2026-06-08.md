# PM Meeting Brief — June 8, 2026

**Project:** Tekno-Phantom-Agent  
**Meeting type:** Progress update  
**Prepared by:** Megh

---

## 1. HITL (Human-in-the-Loop) — Status Update

### What It Is (one sentence for context)
HITL is the mechanism where, when the agent gets completely stuck on a step, instead of failing the entire test run, it pauses the browser, lets a human take over and perform that action manually, then resumes automated execution from the next step.

---

### What Is Working

**The core flow works end to end:**

1. Agent tries a step — all automatic recovery options are exhausted
2. Run pauses at status `waiting_for_input` — browser stays open
3. User sees the live browser in the UI (VNC viewer)
4. A JavaScript recorder is injected silently into the page
5. User clicks/types/selects to perform the blocked action manually
6. User clicks "I'm done — Resume"
7. The system reads the recorded interactions, marks the step as `human_recovered`, and the run continues from the next step automatically
8. The interaction is saved to the recovery store — **the next time the same failure occurs on the same app, the system replays it automatically** without needing human input again

**Impact so far:**
- Zero-failure guarantee: no run is permanently stuck. There is always a human path to unblock it
- Recovery learning: each human intervention trains the system. Over time, HITL sessions become fully automated
- The entire record → normalize → replay pipeline is implemented and operational

---

### What Is Not Yet Stable

| Issue | Explanation | Impact |
|---|---|---|
| Shadow DOM events not captured | Some modern apps (React, Angular) render certain components inside a "shadow root" — a separate DOM layer. The JS recorder is attached to the main document and cannot see events inside shadow roots | Human actions on these components are not recorded. Replay fails silently |
| Iframe interactions not captured | If the blocked element is inside an embedded iframe (e.g., a payment form, embedded widget), the recorder cannot access it due to browser security restrictions | Iframe-based flows cannot be HITL-recovered |
| Replay accuracy at ~85% | When a recorded session is replayed on a future run, 15% of the time the page has rendered differently enough that the replay fails | These cases fall back to asking the human again |
| No capture for drag or file upload | The JS recorder only captures click, type, and select events | If the blocked action was a drag-and-drop or file picker, HITL records nothing useful |

**Bottom line on HITL:** The main flow is solid. The gaps are specific — shadow DOM, iframes, and non-click interactions. These affect a minority of apps but are real blockers when encountered.

---

## 2. Current Problems With Overall Execution Behaviour

### Problem A — Perception Coverage Gaps (Highest Impact)

The agent identifies elements on the page by reading their ARIA labels, visible text, placeholder text, and ID attributes. This works well on standard web apps.

**Where it breaks down:**
- **Icon-only buttons** — a button that is just a gear icon with no label and no `aria-label`. The agent sees it but cannot score it against the step's intent, so it skips it
- **Unlabeled inputs** — an input field with no `<label>`, no `placeholder`, and no `aria-label`. Agent cannot tell what it's for
- **Shadow DOM components** — custom web components (common in design systems like Salesforce, Material, SAP) render their internals in shadow roots. Current `inspect_page()` does not traverse these
- **Canvas and WebGL** — no DOM elements at all. Not solvable without screenshot-based vision

**Frequency:** Approximately 15–20% of steps on modern enterprise apps hit this. These steps either fall to HITL or fail.

---

### Problem B — Plan Quality on Complex Tasks

When a task has more than 12–15 steps, the LLM plan is less reliable:
- Steps are occasionally merged (two actions become one, the second is silently dropped)
- The JSON output gets truncated at the token limit (particularly with OpenAI)
- Verification steps are generated for elements that don't exist yet at that point in the flow

**Frequency:** Low on simple flows (login, form fill). Increases significantly on multi-page, multi-action workflows.

---

### Problem C — Replay Context Mismatch

When a previously saved recovery recipe is replayed, it can select the wrong recipe if the page state looks similar to a different saved session. The context matching works on URL path and visible element text, but two pages in the same app often share these.

**Frequency:** Low — only affects apps with many similar-looking pages (e.g., multi-step wizards, repeated form patterns).

---

### Problem D — Drag-and-Drop Edge Cases

The three-strategy drag cascade handles most apps, but apps that check `event.isTrusted` (a browser security flag that synthetic events cannot satisfy) cannot be automated. This is a hard browser limitation, not something the agent can fix.

---

## 3. What To Focus On This Week

**Recommendation: Two tracks in parallel.**

### Track 1 — Perception Expansion (Highest ROI)
**Goal:** Reduce the 15–20% of steps that fail because the agent can't identify the element.

**Specific work:**
- Add shadow DOM traversal to `inspect_page()` — this alone will unblock a significant set of failures
- Add `aria-description` and `title` attribute scoring to the perception engine — helps with icon buttons that at least have a tooltip

**Why now:** This is the root cause of the majority of current failures. Every other recovery layer (HITL, recipe replay) is downstream of perception. Improving perception reduces the frequency of needing HITL at all.

**Estimated effort:** 3–4 days

---

### Track 2 — HITL Replay Stability (Consolidate What Was Built)
**Goal:** Bring replay accuracy from 85% to 95%+.

**Specific work:**
- Improve snapshot diffing so recipes are only replayed when the page state genuinely matches
- Add a confidence score to replay — if below threshold, ask human again rather than replaying incorrectly

**Why now:** HITL was just completed. Stabilizing replay now, while the code is fresh, prevents it from becoming technical debt.

**Estimated effort:** 2–3 days

---

## 4. Overall Status — Working / Limited / Not Working

### What Is Working Well

| Feature | Notes |
|---|---|
| Basic execution (click, type, select, navigate, wait) | Reliable on standard web apps with proper ARIA labeling |
| Semantic routing | Steps with ARIA-described elements (login forms, standard buttons) work with high reliability |
| Perception engine | Correctly identifies elements in ~80–85% of cases across tested apps |
| HITL pause and resume | Core flow end-to-end: pause → human acts → resume → recipe saved |
| Selector memory | Historical selectors remembered, reduces re-failures on known elements |
| Test case CRUD + suite execution | Stable — create, run, manage test cases and suites |
| HTML run reports | Generated correctly, readable by non-technical stakeholders |
| JWT authentication | Stable — login, cookie sessions, CSRF protection |
| Docker deployment | Working — backend, brain, frontend, nginx all containerize correctly |
| CI pipelines | Backend and frontend CI pass on every PR |

---

### What Is Working But Limited

| Feature | Limitation |
|---|---|
| Perception engine | Fails on icon-only buttons, unlabeled inputs, and shadow DOM components |
| Drag-and-drop | Works on ~80% of apps. Fails on apps using isTrusted event checks |
| HITL replay | Works 85% of the time. 15% of replays fail due to page state differences |
| Plan generation (complex tasks) | Reliable on <12 step flows. Drops steps or truncates on longer workflows |
| Verify image | Baseline comparison works but baseline management (creating/updating baselines) has no UI |
| Browser viewer (VNC) | Works in Docker on Linux only. Unavailable on Windows development machines |

---

### What Is Not Yet Working

| Feature | Status |
|---|---|
| Shadow DOM traversal in perception | Not implemented. Blocking for apps built with design systems (Salesforce, SAP, Material Web Components) |
| Post-action verification | After clicking "Submit", the agent does not check whether the expected result happened. It moves to the next step regardless |
| HITL for drag / file upload | Recorder does not capture these interaction types |
| Plan quality feedback loop | Execution failures do not feed back to improve future plan generation |
| Parallel test execution | Suite runs are sequential only. One test case at a time |
| Automated screenshot baseline management | No UI to create or update image baselines for verify_image steps |

---

## Summary for the Meeting (30-second version)

> "HITL is working — the core flow of pause, human takeover, and resume is complete and the recovery is being learned automatically. The main gaps are shadow DOM and iframes, which affect a subset of enterprise apps.  
> 
> The biggest current execution problem is perception coverage — the agent cannot identify icon buttons or components inside shadow DOM. This is causing 15–20% of steps to need recovery on modern apps.  
> 
> This week I'd like to focus on shadow DOM support in the perception engine, because that will reduce failures at the source rather than adding more recovery layers on top. In parallel, I'll tighten up HITL replay accuracy while it's still fresh.  
> 
> In terms of overall state: basic flows, auth, reporting, and deployment are solid. The gaps are specifically around unlabeled UI elements and complex multi-step plan generation."

