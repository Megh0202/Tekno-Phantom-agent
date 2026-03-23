"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams, useRouter } from "next/navigation";

import styles from "./page.module.css";

type JsonObject = Record<string, unknown>;
type TestTab = "description" | "tests" | "attachments";

type TestCaseState = {
  test_case_id: string;
  name: string;
  description?: string;
  prompt?: string;
  parent_folder_id?: string | null;
  start_url?: string | null;
  steps: Record<string, unknown>[];
  test_data?: JsonObject;
  selector_profile?: Record<string, string[]>;
};

type PlanGenerateResponse = {
  run_name: string;
  start_url?: string | null;
  steps: Record<string, unknown>[];
};

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8080";
const ADMIN_API_TOKEN = process.env.NEXT_PUBLIC_ADMIN_API_TOKEN?.trim() ?? "";

function buildApiHeaders(options?: { json?: boolean }): HeadersInit {
  const headers: Record<string, string> = {};
  if (options?.json) headers["Content-Type"] = "application/json";
  if (ADMIN_API_TOKEN) headers["X-Admin-Token"] = ADMIN_API_TOKEN;
  return headers;
}

async function parseError(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: string };
    if (body.detail) return body.detail;
  } catch {
    // Ignore parse failures and use fallback.
  }
  return `${response.status} ${response.statusText}`;
}

function formatPlanValue(value: unknown): string {
  if (value === null || value === undefined) return "null";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function formatPlanStep(step: Record<string, unknown>): string {
  const rawType = typeof step.type === "string" ? step.type : "step";
  const typeLabel = rawType
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
  const details = Object.entries(step)
    .filter(([key]) => key !== "type")
    .map(([key, value]) => `${key}=${formatPlanValue(value)}`)
    .join(", ");
  return details ? `${typeLabel}: ${details}` : typeLabel;
}

function normalizeManualStepLines(raw: string): string[] {
  return raw
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => Boolean(line));
}

function buildTaskFromManualStepLines(lines: string[]): string {
  return lines.map((line, index) => `${index + 1}. ${line}`).join("\n");
}

function extractUserStepLinesFromPrompt(prompt: string): string[] {
  return prompt
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => Boolean(line))
    .map((line) => line.replace(/^\d+[\).\s-]+/, "").trim());
}

export default function TestCaseDetailsPage() {
  const router = useRouter();
  const params = useParams<{ testCaseId: string }>();
  const testCaseId = decodeURIComponent(params?.testCaseId ?? "");

  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [isRunning, setIsRunning] = useState(false);
  const [activeTab, setActiveTab] = useState<TestTab>("tests");
  const [requestError, setRequestError] = useState<string | null>(null);
  const [requestInfo, setRequestInfo] = useState<string | null>(null);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [manualStepsInput, setManualStepsInput] = useState("");
  const [newStepLine, setNewStepLine] = useState("");
  const [editingStepIndex, setEditingStepIndex] = useState<number | null>(null);
  const [editingStepValue, setEditingStepValue] = useState("");
  const [startUrl, setStartUrl] = useState<string | null>(null);
  const [testData, setTestData] = useState<JsonObject>({});
  const [selectorProfile, setSelectorProfile] = useState<Record<string, string[]>>({});

  const testCount = useMemo(() => normalizeManualStepLines(manualStepsInput).length, [manualStepsInput]);

  const loadTestCase = useCallback(async () => {
    if (!testCaseId) return;
    setIsLoading(true);
    setRequestError(null);
    try {
      const response = await fetch(`${API_BASE_URL}/api/test-cases/${testCaseId}`, {
        cache: "no-store",
        headers: buildApiHeaders(),
      });
      if (!response.ok) {
        throw new Error(await parseError(response));
      }
      const detail = (await response.json()) as TestCaseState;
      setName(detail.name ?? "");
      setDescription(detail.description ?? "");
      setStartUrl(detail.start_url ?? null);
      setTestData(detail.test_data ?? {});
      setSelectorProfile(detail.selector_profile ?? {});
      const promptStepLines = extractUserStepLinesFromPrompt((detail.prompt ?? "").trim());
      if (promptStepLines.length > 0) {
        setManualStepsInput(promptStepLines.join("\n"));
      } else {
        setManualStepsInput(
          (detail.steps ?? [])
            .map((step, index) => `${index + 1}. ${formatPlanStep(step as Record<string, unknown>)}`)
            .join("\n"),
        );
      }
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "Failed to load test case");
    } finally {
      setIsLoading(false);
    }
  }, [testCaseId]);

  useEffect(() => {
    void loadTestCase();
  }, [loadTestCase]);

  function addStepLine(): void {
    const normalized = newStepLine.trim();
    if (!normalized) {
      setRequestError("Enter a test step before creating.");
      return;
    }
    setRequestError(null);
    setManualStepsInput((previous) => (previous.trim() ? `${previous.trim()}\n${normalized}` : normalized));
    setNewStepLine("");
  }

  function startEditingStep(index: number, currentValue: string): void {
    setEditingStepIndex(index);
    setEditingStepValue(currentValue);
  }

  function cancelEditingStep(): void {
    setEditingStepIndex(null);
    setEditingStepValue("");
  }

  function saveEditedStep(): void {
    if (editingStepIndex === null) return;
    const normalized = editingStepValue.trim();
    if (!normalized) {
      setRequestError("Step text cannot be empty.");
      return;
    }
    const lines = normalizeManualStepLines(manualStepsInput);
    if (editingStepIndex < 0 || editingStepIndex >= lines.length) return;
    lines[editingStepIndex] = normalized;
    setManualStepsInput(lines.join("\n"));
    setEditingStepIndex(null);
    setEditingStepValue("");
    setRequestError(null);
  }

  function deleteStep(index: number): void {
    const lines = normalizeManualStepLines(manualStepsInput);
    if (index < 0 || index >= lines.length) return;
    lines.splice(index, 1);
    setManualStepsInput(lines.join("\n"));
    if (editingStepIndex === index) {
      cancelEditingStep();
    } else if (editingStepIndex !== null && editingStepIndex > index) {
      setEditingStepIndex(editingStepIndex - 1);
    }
  }

  async function saveTestCase(): Promise<void> {
    const lines = normalizeManualStepLines(manualStepsInput);
    if (!name.trim()) {
      setRequestError("Test case name is required.");
      return;
    }
    if (lines.length === 0) {
      setRequestError("Add at least one test step.");
      return;
    }

    setRequestError(null);
    setRequestInfo(null);
    setIsSaving(true);
    try {
      const task = buildTaskFromManualStepLines(lines);
      const planResponse = await fetch(`${API_BASE_URL}/api/plan`, {
        method: "POST",
        headers: buildApiHeaders({ json: true }),
        body: JSON.stringify({
          task,
          max_steps: 300,
          test_data: testData,
          selector_profile: selectorProfile,
        }),
      });
      if (!planResponse.ok) {
        throw new Error(await parseError(planResponse));
      }
      const plan = (await planResponse.json()) as PlanGenerateResponse;

      const response = await fetch(`${API_BASE_URL}/api/test-cases/${testCaseId}`, {
        method: "PUT",
        headers: buildApiHeaders({ json: true }),
        body: JSON.stringify({
          name: name.trim(),
          description: description.trim(),
          prompt: task,
          start_url: startUrl ?? plan.start_url ?? null,
          steps: plan.steps,
          test_data: testData,
          selector_profile: selectorProfile,
        }),
      });
      if (!response.ok) {
        throw new Error(await parseError(response));
      }
      setRequestInfo("Test case saved.");
      await loadTestCase();
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "Failed to save test case");
    } finally {
      setIsSaving(false);
    }
  }

  async function runTestCase(): Promise<void> {
    setRequestError(null);
    setRequestInfo(null);
    setIsRunning(true);
    try {
      const response = await fetch(`${API_BASE_URL}/api/test-cases/${testCaseId}/run`, {
        method: "POST",
        headers: buildApiHeaders(),
      });
      if (!response.ok) {
        throw new Error(await parseError(response));
      }
      const run = (await response.json()) as { run_id: string; run_name: string };
      setRequestInfo(`Run started: ${run.run_id} (${run.run_name})`);
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "Failed to run test case");
    } finally {
      setIsRunning(false);
    }
  }

  return (
    <main className={styles.page}>
      <header className={styles.header}>
        <div>
          <button type="button" className={styles.linkButton} onClick={() => router.push("/")}>
            Back to Dashboard
          </button>
          <h1>{name || "Test Details"}</h1>
          <p className={styles.meta}>Test Case ID: {testCaseId}</p>
        </div>
        <div className={styles.headerActions}>
          <button type="button" className={styles.secondaryButton} onClick={() => void loadTestCase()}>
            Refresh
          </button>
          <button type="button" className={styles.secondaryButton} onClick={() => void runTestCase()} disabled={isRunning}>
            {isRunning ? "Running..." : "Run"}
          </button>
          <button type="button" className={styles.primaryButton} onClick={() => void saveTestCase()} disabled={isSaving}>
            {isSaving ? "Saving..." : "Save"}
          </button>
        </div>
      </header>

      {requestError ? <p className={styles.errorText}>{requestError}</p> : null}
      {requestInfo ? <p className={styles.infoText}>{requestInfo}</p> : null}
      {isLoading ? <p className={styles.meta}>Loading test case...</p> : null}

      <section className={styles.panel}>
        <div className={styles.tabs}>
          <button
            type="button"
            className={`${styles.tabButton} ${activeTab === "description" ? styles.tabActive : ""}`}
            onClick={() => setActiveTab("description")}
          >
            Description
          </button>
          <button
            type="button"
            className={`${styles.tabButton} ${activeTab === "tests" ? styles.tabActive : ""}`}
            onClick={() => setActiveTab("tests")}
          >
            Tests {testCount}
          </button>
          <button
            type="button"
            className={`${styles.tabButton} ${activeTab === "attachments" ? styles.tabActive : ""}`}
            onClick={() => setActiveTab("attachments")}
          >
            Attachments
          </button>
        </div>

        {activeTab === "description" ? (
          <div className={styles.formGrid}>
            <label className={styles.fieldLabel}>
              <span>Name</span>
              <input value={name} onChange={(event) => setName(event.target.value)} />
            </label>
            <label className={styles.fieldLabel}>
              <span>Description</span>
              <textarea rows={4} value={description} onChange={(event) => setDescription(event.target.value)} />
            </label>
          </div>
        ) : null}

        {activeTab === "tests" ? (
          <div className={styles.testsPane}>
            <ol className={styles.list}>
              {normalizeManualStepLines(manualStepsInput).map((line, index) => (
                <li key={`step-${index}`} className={styles.stepItem}>
                  <div className={styles.stepTopRow}>
                    <span className={styles.stepIndex}>{index + 1}.</span>
                    {editingStepIndex === index ? (
                      <input
                        className={styles.stepEditInput}
                        value={editingStepValue}
                        onChange={(event) => setEditingStepValue(event.target.value)}
                      />
                    ) : (
                      <span className={styles.stepText}>{line}</span>
                    )}
                    <div className={styles.stepActions}>
                      {editingStepIndex === index ? (
                        <>
                          <button type="button" className={styles.stepActionPrimary} onClick={saveEditedStep}>
                            Save
                          </button>
                          <button type="button" className={styles.stepActionButton} onClick={cancelEditingStep}>
                            Cancel
                          </button>
                        </>
                      ) : (
                        <>
                          <button
                            type="button"
                            className={styles.stepActionButton}
                            onClick={() => startEditingStep(index, line)}
                          >
                            Edit
                          </button>
                          <button
                            type="button"
                            className={styles.stepActionDanger}
                            onClick={() => deleteStep(index)}
                          >
                            Delete
                          </button>
                        </>
                      )}
                    </div>
                  </div>
                </li>
              ))}
            </ol>
            <div className={styles.newRow}>
              <input
                value={newStepLine}
                onChange={(event) => setNewStepLine(event.target.value)}
                placeholder="Add new test"
              />
              <button type="button" className={styles.primaryButton} onClick={addStepLine}>
                Create
              </button>
            </div>
          </div>
        ) : null}

        {activeTab === "attachments" ? <p className={styles.meta}>Attachments UI can be added next.</p> : null}
      </section>
    </main>
  );
}
