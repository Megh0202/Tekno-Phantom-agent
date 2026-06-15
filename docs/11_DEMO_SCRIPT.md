# Document 11: Demo Script

> **Purpose:** A scripted walkthrough you can follow when showing this project to a PM, investor, or new team member. Covers what to say, what to click, what to show, and what to do if something goes wrong.  
> **Time:** 5-minute version (below) and 12-minute deep-dive version.

---

## What Makes a Good Demo of This System

The key insight to communicate is: **this is not a record-and-playback tool**. Record-and-playback tools (like Cypress test recorder, Selenium IDE) record exact CSS selectors and break the moment the page HTML changes. This system understands what the element IS — not what its CSS class is — and finds it even after UI changes.

The second thing to show is **recovery**: what happens when the agent gets stuck. The HITL flow (pause → human takes over → resumes → learns) is the most impressive feature because it directly answers the "what if it fails?" question.

---

## Prerequisites Before the Demo

1. **System is running** — backend, brain, and frontend are up. `curl http://localhost/api/health` returns `{"status": "ok"}`.

2. **An Anthropic API key is set** in `brain/.env` — the demo needs real LLM plan generation.

3. **The target app is accessible** — you need a web app to test against. Options:
   - A simple public app like [https://the-internet.herokuapp.com](https://the-internet.herokuapp.com) (has login, form, drag-and-drop pages)
   - Your company's own staging environment
   - A locally running test app

4. **Browser viewer is enabled** (for HITL demo) — `BROWSER_VIEWER_ENABLED=true` and `PLAYWRIGHT_HEADLESS=false` in `.env`. This requires Docker on Linux.

5. **Reset any previous state** — if you've demo'd before, clear the run list so the UI looks clean:
   ```bash
   # Optional: remove old runs so the list is empty
   docker compose exec backend sqlite3 /app/data/run_store.sqlite3 "DELETE FROM runs;"
   ```

---

## 5-Minute Demo Script

### Section 1: The Problem (30 seconds)

**Say:** "Traditional test automation tools record exactly which element to click, down to the CSS class. The moment the developer renames a class or restructures the HTML, the test breaks — you get an error like 'element not found' and someone has to manually update the selector. On a large app with hundreds of tests, this maintenance becomes a full-time job."

**Don't show anything yet** — this is just setup for why the demo matters.

---

### Section 2: Create and Run a Test (2 minutes)

**Navigate to the frontend** (e.g., `http://localhost` or your deploy URL).

**Click "New Test Case"** or go to "Run" → "New Run".

**In the task description, type:**
```
Go to https://the-internet.herokuapp.com/login.
Type "tomsmith" in the Username field.
Type "SuperSecretPassword!" in the Password field.
Click the Login button.
Verify the text "You logged into a secure area!" is on the page.
```

**Say while typing:** "I'm describing the test in plain English — exactly how I'd describe it to a QA engineer. There are no CSS selectors, no element IDs, no code. The system will figure out which elements to interact with from this description alone."

**Click Run** (or "Generate Plan and Execute").

**While it runs, say:**
> "What just happened behind the scenes: the system sent this task to the LLM, which analyzed it and produced a step-by-step execution plan in JSON format. That plan uses semantic targets — 'the textbox with accessible name Username', 'the button with role button and text Login' — not CSS selectors. The backend is now reading the live page, scoring every interactive element against those semantic descriptions, and picking the best match."

**Point to the step list as steps complete:**
> "Each step shows the element it found, the confidence level the perception engine gave it, and whether it succeeded. Step 2 says 'confidence=high score=82' — that means perception found the Username field with 82% confidence, which is above the threshold to act directly without needing fallback."

**When it finishes:**
> "The run completed. Every step passed. This took about 15 seconds — it navigated, filled two fields, clicked login, and verified the result. All from a plain English description with no code."

---

### Section 3: Show That It's Not Fragile (1 minute)

**The key point to make here:** Most automation tools would break if the username field changed from `<input id="username">` to `<input id="user_name">`. This system wouldn't, because it found the field by its label ("Username"), not by its ID.

**If you have access to the source app and can make an element change:**
1. Change the `id` attribute of the username field
2. Re-run the same test
3. It still passes — because perception uses the label text, not the ID

**If you can't change the source app, say this instead:**
> "If someone changed the CSS class, ID, or DOM structure of this page, the test would still pass. The system identified the username field by reading its label text and ARIA role — the same way a screen reader or a human would find it. It's looking for 'the textbox labeled Username', not '#username-input-field-v2'."

---

### Section 4: The Recovery Story (1.5 minutes) — Only if Browser Viewer is Enabled

**Say:** "Now let me show what happens when the agent gets completely stuck."

**Create a new run with an intentionally broken step:**
```
Go to https://the-internet.herokuapp.com/login.
Click the element with id "button-that-does-not-exist".
```

**Run it and show the paused state:**

When the run hits `waiting_for_input`, the UI should show:
- The run status is "Paused — waiting for human input"
- The VNC viewer shows the live browser
- A "Provide Selector" input or "Resume" button

**Say:**
> "The agent tried every option it had — it checked its memory of past selectors, it asked the LLM for suggestions, it ran its perception engine. None of them found the element because it genuinely doesn't exist. Instead of failing the entire run, it paused here with the browser kept alive and the page exactly as it was."

**Point to the VNC viewer:**
> "This is the actual browser running inside the Docker container right now. The user can see exactly what the agent sees. They can type in the correct selector, or — and this is the key feature — they can simply perform the action themselves in this live browser."

**Click "Provide Selector" and type a real element's selector**, e.g., `#username`:
> "I'll give it the correct selector. The system will retry the step with what I provided, continue the run, and — importantly — remember this selector for next time. The next time this run encounters this same failure, it won't need to ask me again."

---

### Section 5: The Summary (30 seconds)

**Show the run report** (the HTML artifact linked from the run page):
> "Every run generates a readable HTML report — step by step, what succeeded, what failed, screenshots at the moment of failure, and a one-sentence LLM summary at the top. This report is for non-technical stakeholders who need to understand what happened without reading logs."

**Close with:**
> "The system has three main qualities: it understands pages semantically so it's resilient to UI changes, it recovers automatically from most failures, and when it can't recover automatically, a human can unblock it without stopping the run — and the system learns from that intervention so it won't need help again."

---

## 12-Minute Deep-Dive Addition

Use these sections if you have a technical audience who wants to understand how it works.

### The Perception Engine (3 minutes)

Open a terminal and show the step log for a recent run:

```bash
cat artifacts/<run-id>/step-001.log | python3 -m json.tool
```

Point to the `perception` section:

```json
"perception": {
  "confidence": "high",
  "score": 82,
  "grounded_selector": "#username",
  "element": {
    "tag": "input",
    "role": "textbox",
    "aria": "",
    "label": "Username",
    "placeholder": "username"
  },
  "scored_elements": [
    { "tag": "input", "score": 82, "selector": "#username" },
    { "tag": "input", "score": 45, "selector": "#password" }
  ]
}
```

**Say:**
> "The perception engine read the live DOM, found every interactive element on the page, and scored each one against the step's intent. The username field scored 82 — it matched on label text, ARIA role, and placeholder. The password field scored 45 — it has the right role but the wrong label. The engine picked the 82-score match with 'high' confidence and used it."

**Point to the confidence levels:**
> "There are four confidence levels: unique, high, medium, ambiguous. High and unique means the agent acts immediately. Medium means it validates first — it checks that the element's tag and label actually make sense for what it's trying to do. Ambiguous means multiple elements scored similarly — the agent falls back to a slower, more careful path."

---

### The 7-Layer Fallback (3 minutes)

Draw this or show it on a slide:

```
Step arrives
    ↓
1. Recipe replay: "Has a human fixed this exact failure before? Replay it."
    ↓ (no saved recipe)
2. Semantic routing: "Does this step have a full semantic target? Go fast."
    ↓ (or slow path if not)
3. Perception: "Read the live DOM, score elements, find best match."
    ↓ (no confident match)
4. Selector memory: "Have I clicked this before on this domain? Try those selectors."
    ↓ (no remembered selector)
5. LLM selector repair: "Ask the brain: suggest better selectors from this page."
    ↓ (LLM suggestions also failed)
6. Retry loop: "Try all candidates in order, with timeouts."
    ↓ (all failed)
7. HITL pause: "Ask a human. Keep the browser open."
```

**Say:**
> "Each layer only runs if the previous one failed. A step on an app we've tested before usually resolves at layer 4 (selector memory) or layer 2 (semantic routing). A brand new app goes through all layers. HITL is the guaranteed path — no run can be permanently stuck because there's always a human fallback."

---

### The LLM Prompts (2 minutes)

Show `docs/05_LLM_PROMPT_INVENTORY.md` and point to the plan generation prompt:

**Say:**
> "Every constraint in this prompt exists because it was added after something broke. The line 'Do NOT include a selector field in any step' was added because the LLM would generate brittle CSS selectors and the executor would use those instead of asking perception. The 'Do NOT generate CSS selectors, XPath, nth-child paths' was added because even after removing the selector field, the LLM would still generate selectors in other fields."

**Point to the SEMANTIC TARGET CONTRACT section:**
> "This is the most important part. By requiring the LLM to describe elements by their accessible name and ARIA role — not by their position in the DOM — we force the plan to be resilient to layout changes. The element's role and label rarely change; its CSS class and DOM depth change all the time."

---

## What to Do If Something Goes Wrong During the Demo

| Problem | What to say | What to do |
|---|---|---|
| The plan generation takes too long | "The brain is calling the LLM — this is a one-time cost per run. In production we'd cache common plans." | Wait — it will complete. Anthropic usually responds in 5-10 seconds. |
| A step fails unexpectedly | "That's actually interesting — let me show you what the system does when it fails." | Pivot to the HITL recovery demo (Section 4). |
| The browser viewer is black | "The viewer requires the Linux Docker environment — on this machine we'd see the browser window directly." | Show the step logs and screenshots instead. |
| LLM returns a bad plan | "The plan quality depends on how clearly the task is described. Let me show the step expansion step." | Regenerate with a more explicit task description. |
| The system is completely unreachable | "Let me grab the logs while I restart." | `docker compose restart backend`, wait 30s, retry. |

---

## The 30-Second Elevator Pitch

For when you only have 30 seconds:

> "It's a browser automation agent that understands web pages the way a human does — by reading labels and roles, not CSS selectors. You describe a test in plain English, it generates an execution plan and runs it. When it gets stuck, instead of failing, it pauses and lets a human take over the live browser — and it learns from that so it never needs human help on the same problem again."
