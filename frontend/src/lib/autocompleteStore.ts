/**
 * Per-field suggestion store backed by localStorage.
 * Each field keeps up to MAX_ENTRIES recent values.
 * The "prompt" field is pre-seeded with common web automation patterns.
 */

const MAX_ENTRIES = 50;

const PROMPT_SEEDS: string[] = [
  "Open https:// and verify the page loads",
  "Navigate to https:// and click the login button",
  "Go to https://example.com, wait for full load, then verify h1 contains 'Example Domain'",
  "Log in with username and password",
  "Sign up with email and password",
  "Click the Submit button",
  "Click the Search button and verify results appear",
  "Type in the search box and press Enter",
  "Fill in the contact form and submit",
  "Verify the heading says",
  "Verify the page title is",
  "Select an option from the dropdown",
  "Scroll down to the footer",
  "Wait for the modal to appear and close it",
  "Open the menu and navigate to",
  "Upload a file using the file input",
  "Check that the error message says",
  "Confirm the success notification appears",
];

function storageKey(fieldKey: string): string {
  return `__ac:${fieldKey}`;
}

/**
 * Read all stored suggestions for a field.
 * Falls back to seed data for the "prompt" field when nothing is stored yet.
 */
export function getEntries(fieldKey: string): string[] {
  if (typeof window === "undefined") return fieldKey === "prompt" ? PROMPT_SEEDS : [];
  try {
    const raw = localStorage.getItem(storageKey(fieldKey));
    if (raw === null) {
      return fieldKey === "prompt" ? PROMPT_SEEDS : [];
    }
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as string[]) : [];
  } catch {
    return [];
  }
}

/**
 * Persist a new value for a field (most-recent first, deduped).
 * Silently ignores values shorter than 3 characters.
 */
export function addEntry(fieldKey: string, value: string): void {
  if (typeof window === "undefined") return;
  const trimmed = value.trim();
  if (trimmed.length < 3) return;
  try {
    const current = getEntries(fieldKey).filter((s) => s !== trimmed);
    const updated = [trimmed, ...current].slice(0, MAX_ENTRIES);
    localStorage.setItem(storageKey(fieldKey), JSON.stringify(updated));
  } catch {
    // Quota exceeded or private browsing — ignore silently.
  }
}
