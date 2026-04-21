/**
 * Domain-specific word list for web automation prompt completion.
 * Used for real-time prefix-based word suggestions.
 */
export const WORD_LIST: readonly string[] = [
  // ── Actions ──────────────────────────────────────────────────────────────
  "navigate", "click", "type", "enter", "submit", "verify", "check", "scroll",
  "wait", "hover", "drag", "drop", "select", "fill", "assert", "expect",
  "open", "close", "visit", "find", "locate", "search", "press", "tap",
  "confirm", "cancel", "dismiss", "accept", "upload", "download", "refresh",
  "reload", "login", "logout", "signin", "signup", "register", "authenticate",
  "capture", "screenshot", "resize", "maximize", "minimize", "focus", "blur",
  "clear", "copy", "paste", "highlight", "inspect", "intercept", "mock",

  // ── Elements ─────────────────────────────────────────────────────────────
  "button", "input", "form", "link", "menu", "modal", "dialog", "header",
  "footer", "page", "field", "checkbox", "radio", "dropdown", "textarea",
  "image", "table", "row", "column", "tab", "panel", "section", "sidebar",
  "navbar", "navigation", "breadcrumb", "pagination", "tooltip", "popup",
  "notification", "alert", "banner", "card", "list", "item", "option",
  "label", "placeholder", "heading", "title", "text", "content", "body",
  "container", "wrapper", "overlay", "spinner", "loader", "badge", "chip",
  "accordion", "carousel", "stepper", "progress", "toggle", "switch",

  // ── Assertions ────────────────────────────────────────────────────────────
  "contains", "equals", "matches", "exists", "visible", "hidden", "enabled",
  "disabled", "checked", "selected", "empty", "present", "absent", "loaded",

  // ── Common connector words ────────────────────────────────────────────────
  "the", "this", "that", "there", "then", "them", "they", "those", "these",
  "when", "where", "which", "with", "from", "into", "onto", "after", "before",
  "until", "while", "and", "not", "should", "must", "will", "can", "could",
  "have", "been", "using", "via", "through", "without", "inside", "outside",

  // ── URL / web terms ───────────────────────────────────────────────────────
  "https", "http", "localhost", "example", "google", "github", "wikipedia",
  "home", "about", "contact", "search", "profile", "settings", "dashboard",
  "admin", "api", "endpoint", "redirect", "response", "request", "cookie",

  // ── Test / QA terms ───────────────────────────────────────────────────────
  "test", "verify", "validate", "assert", "expect", "confirm", "ensure",
  "step", "flow", "scenario", "case", "suite", "automation", "browser",
  "playwright", "selenium", "cypress", "result", "pass", "fail", "skip",
  "timeout", "retry", "flaky", "stable", "baseline", "snapshot", "report",

  // ── Field / data values ───────────────────────────────────────────────────
  "username", "password", "email", "name", "address", "phone", "number",
  "value", "data", "error", "success", "message", "invalid", "required",
  "optional", "default", "placeholder", "format", "pattern", "length",

  // ── Modifiers & directions ────────────────────────────────────────────────
  "first", "last", "next", "previous", "top", "bottom", "left", "right",
  "center", "middle", "above", "below", "inside", "outside", "visible",
  "correct", "incorrect", "valid", "invalid", "full", "partial", "exact",
];

/**
 * Extract the word being typed at cursor position.
 * Stops at whitespace, punctuation (except apostrophe), or start of string.
 */
export function getCurrentWord(
  text: string,
  cursorPos: number,
): { word: string; start: number; end: number } {
  let start = cursorPos;
  while (start > 0 && !/[\s,.()\[\]{}<>'"!?;:]/.test(text[start - 1])) {
    start--;
  }
  return { word: text.slice(start, cursorPos), start, end: cursorPos };
}

/**
 * Replace the current word in text with the selected suggestion.
 * Appends a space so the user can keep typing immediately.
 */
export function replaceCurrentWord(
  text: string,
  wordStart: number,
  wordEnd: number,
  replacement: string,
): string {
  return text.slice(0, wordStart) + replacement + " " + text.slice(wordEnd);
}

/**
 * Prefix-match the current word against the dictionary.
 * Returns up to `max` results, sorted by length (shortest first for quick picks).
 */
export function getSuggestions(word: string, max = 8): string[] {
  if (word.length < 2) return [];
  const q = word.toLowerCase();
  return WORD_LIST.filter((w) => w.startsWith(q) && w !== q)
    .sort((a, b) => a.length - b.length)
    .slice(0, max);
}
