"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams, useRouter } from "next/navigation";

import {
  apiFetch as fetch,
  buildApiHeaders,
} from "@/lib/api-auth";
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

type AgentConfig = {
  jwt_auth_enabled?: boolean;
};

type AuthUser = {
  id: number;
  email: string;
  role: string;
  is_active: boolean;
};

type AuthSessionResponse = {
  authenticated: boolean;
  user: AuthUser;
  expires_in: number;
};

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8080";
const ADMIN_API_TOKEN = process.env.NEXT_PUBLIC_ADMIN_API_TOKEN?.trim() ?? "";

function formatApiDetail(detail: unknown): string {
  if (typeof detail === "string" && detail.trim()) {
    return detail.trim();
  }
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item) => formatApiDetail(item))
      .filter((item) => Boolean(item));
    if (parts.length) return parts.join("; ");
    return "";
  }
  if (detail && typeof detail === "object") {
    const payload = detail as Record<string, unknown>;
    const directMessage =
      (typeof payload.message === "string" && payload.message.trim())
      || (typeof payload.error === "string" && payload.error.trim())
      || (typeof payload.detail === "string" && payload.detail.trim());
    const validationErrors = Array.isArray(payload.validation_errors)
      ? payload.validation_errors.map((item) => formatApiDetail(item)).filter((item) => Boolean(item))
      : [];
    const rejectionReasons = Array.isArray(payload.rejection_reasons)
      ? payload.rejection_reasons.map((item) => formatApiDetail(item)).filter((item) => Boolean(item))
      : [];
    const parts = [directMessage, ...validationErrors, ...rejectionReasons].filter((item) => Boolean(item));
    if (parts.length) return parts.join("; ");
    try {
      return JSON.stringify(detail);
    } catch {
      return String(detail);
    }
  }
  if (detail === null || detail === undefined) {
    return "";
  }
  return String(detail);
}

async function parseError(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown; message?: unknown; error?: unknown };
    const detail = formatApiDetail(body.detail);
    if (detail) return detail;
    const message = formatApiDetail(body.message);
    if (message) return message;
    const error = formatApiDetail(body.error);
    if (error) return error;
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

  const [config, setConfig] = useState<AgentConfig | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [isRunning, setIsRunning] = useState(false);
  const [isAuthenticating, setIsAuthenticating] = useState(false);
  const [activeTab, setActiveTab] = useState<TestTab>("tests");
  const [requestError, setRequestError] = useState<string | null>(null);
  const [requestInfo, setRequestInfo] = useState<string | null>(null);
  const [authUser, setAuthUser] = useState<AuthUser | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [authMode, setAuthMode] = useState<"signin" | "signup">("signin");
  const [authEmail, setAuthEmail] = useState("");
  const [authPassword, setAuthPassword] = useState("");
  const [authConfirmPassword, setAuthConfirmPassword] = useState("");

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [manualStepsInput, setManualStepsInput] = useState("");
  const [newStepLine, setNewStepLine] = useState("");
  const [editingStepIndex, setEditingStepIndex] = useState<number | null>(null);
  const [editingStepValue, setEditingStepValue] = useState("");
  const [startUrl, setStartUrl] = useState<string | null>(null);
  const [testData, setTestData] = useState<JsonObject>({});
  const [selectorProfile, setSelectorProfile] = useState<Record<string, string[]>>({});
  const requiresJwtAuth = Boolean(config?.jwt_auth_enabled) && !ADMIN_API_TOKEN;
  const authBlocked = requiresJwtAuth && (!authChecked || !authUser);

  const testCount = useMemo(() => normalizeManualStepLines(manualStepsInput).length, [manualStepsInput]);

  useEffect(() => {
    let disposed = false;

    async function loadConfig(): Promise<void> {
      try {
        const response = await fetch(`${API_BASE_URL}/api/config`, {
          cache: "no-store",
          headers: buildApiHeaders({ adminToken: ADMIN_API_TOKEN }),
        });
        if (!response.ok) {
          throw new Error(await parseError(response));
        }
        const payload = (await response.json()) as AgentConfig;
        if (!disposed) {
          setConfig(payload);
        }
      } catch (error) {
        if (!disposed) {
          setRequestError(error instanceof Error ? error.message : "Failed to load config");
        }
      }
    }

    void loadConfig();
    return () => {
      disposed = true;
    };
  }, []);

  const loadCurrentUser = useCallback(async (): Promise<void> => {
    if (!requiresJwtAuth) {
      setAuthUser(null);
      setAuthChecked(true);
      return;
    }
    try {
      const response = await fetch(`${API_BASE_URL}/auth/me`, {
        cache: "no-store",
        headers: buildApiHeaders({ adminToken: ADMIN_API_TOKEN }),
      });
      if (response.status === 401) {
        setAuthUser(null);
        setAuthChecked(true);
        return;
      }
      if (!response.ok) {
        throw new Error(await parseError(response));
      }
      const payload = (await response.json()) as AuthUser;
      setAuthUser(payload);
      setAuthChecked(true);
    } catch (error) {
      setAuthUser(null);
      setAuthChecked(true);
      setRequestError(error instanceof Error ? error.message : "Failed to load current user");
    }
  }, [requiresJwtAuth]);

  useEffect(() => {
    void loadCurrentUser();
  }, [loadCurrentUser]);

  const loadTestCase = useCallback(async () => {
    if (!testCaseId) return;
    if (requiresJwtAuth && !authUser) {
      setIsLoading(false);
      return;
    }
    setIsLoading(true);
    setRequestError(null);
    try {
      const response = await fetch(`${API_BASE_URL}/api/test-cases/${testCaseId}`, {
        cache: "no-store",
        headers: buildApiHeaders({ adminToken: ADMIN_API_TOKEN }),
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
  }, [authUser, requiresJwtAuth, testCaseId]);

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
        headers: buildApiHeaders({ json: true, adminToken: ADMIN_API_TOKEN }),
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
        headers: buildApiHeaders({ json: true, adminToken: ADMIN_API_TOKEN }),
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
        headers: buildApiHeaders({ adminToken: ADMIN_API_TOKEN }),
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

  async function signIn(): Promise<void> {
    const email = authEmail.trim();
    const password = authPassword;
    if (!email || !password) {
      setRequestError("Enter email and password to sign in.");
      return;
    }

    setRequestError(null);
    setRequestInfo(null);
    try {
      setIsAuthenticating(true);
      const response = await fetch(`${API_BASE_URL}/auth/login`, {
        method: "POST",
        headers: buildApiHeaders({ json: true, adminToken: ADMIN_API_TOKEN }),
        body: JSON.stringify({ email, password }),
      });
      if (!response.ok) {
        throw new Error(await parseError(response));
      }
      const payload = (await response.json()) as AuthSessionResponse;
      setAuthUser(payload.user);
      setAuthChecked(true);
      setAuthPassword("");
      setAuthConfirmPassword("");
      setRequestInfo(`Signed in as ${email}.`);
      await loadTestCase();
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "Failed to sign in");
    } finally {
      setIsAuthenticating(false);
    }
  }

  async function signUp(): Promise<void> {
    const email = authEmail.trim();
    const password = authPassword;
    const confirmPassword = authConfirmPassword;
    if (!email || !password || !confirmPassword) {
      setRequestError("Enter email, password, and confirm password to sign up.");
      return;
    }
    if (password !== confirmPassword) {
      setRequestError("Password and confirm password must match.");
      return;
    }

    setRequestError(null);
    setRequestInfo(null);
    try {
      setIsAuthenticating(true);
      const response = await fetch(`${API_BASE_URL}/auth/register`, {
        method: "POST",
        headers: buildApiHeaders({ json: true, adminToken: ADMIN_API_TOKEN }),
        body: JSON.stringify({ email, password }),
      });
      if (!response.ok) {
        throw new Error(await parseError(response));
      }
      const payload = (await response.json()) as AuthSessionResponse;
      setAuthUser(payload.user);
      setAuthChecked(true);
      setAuthMode("signin");
      setAuthPassword("");
      setAuthConfirmPassword("");
      setRequestInfo(`Account created and signed in as ${email}.`);
      await loadTestCase();
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "Failed to sign up");
    } finally {
      setIsAuthenticating(false);
    }
  }

  async function signOut(): Promise<void> {
    await fetch(`${API_BASE_URL}/auth/logout`, {
      method: "POST",
      headers: buildApiHeaders({ adminToken: ADMIN_API_TOKEN }),
    });
    setAuthUser(null);
    setAuthChecked(true);
    setAuthPassword("");
    setAuthConfirmPassword("");
    setRequestError(null);
    setRequestInfo("Signed out. Sign back in to edit or run this test case.");
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
          <button
            type="button"
            className={styles.secondaryButton}
            onClick={() => void runTestCase()}
            disabled={authBlocked || isRunning}
          >
            {isRunning ? "Running..." : "Run"}
          </button>
          <button
            type="button"
            className={styles.primaryButton}
            onClick={() => void saveTestCase()}
            disabled={authBlocked || isSaving}
          >
            {isSaving ? "Saving..." : "Save"}
          </button>
        </div>
      </header>

      {requiresJwtAuth ? (
        <section className={styles.panel}>
          <div className={styles.authPanel}>
            <div>
              <p className={styles.authTitle}>Access</p>
              <p className={styles.meta}>
                {authUser
                  ? `Signed in as ${authUser.email} (${authUser.role}).`
                  : authMode === "signup"
                    ? "Create your account here. Registration also signs you in."
                    : "Sign in to load, save, and run this test case."}
              </p>
            </div>
            {authUser ? (
              <button type="button" className={styles.secondaryButton} onClick={() => void signOut()}>
                Sign Out
              </button>
            ) : (
              <div className={styles.authForm}>
                <button
                  type="button"
                  className={styles.secondaryButton}
                  onClick={() => setAuthMode("signin")}
                  disabled={isAuthenticating || authMode === "signin"}
                >
                  Sign In
                </button>
                <button
                  type="button"
                  className={styles.secondaryButton}
                  onClick={() => setAuthMode("signup")}
                  disabled={isAuthenticating || authMode === "signup"}
                >
                  Sign Up
                </button>
                <input
                  value={authEmail}
                  onChange={(event) => setAuthEmail(event.target.value)}
                  placeholder="qa@example.com"
                />
                <input
                  type="password"
                  value={authPassword}
                  onChange={(event) => setAuthPassword(event.target.value)}
                  placeholder="Enter password"
                />
                {authMode === "signup" ? (
                  <input
                    type="password"
                    value={authConfirmPassword}
                    onChange={(event) => setAuthConfirmPassword(event.target.value)}
                    placeholder="Confirm password"
                  />
                ) : null}
                <button
                  type="button"
                  className={styles.primaryButton}
                  onClick={() => void (authMode === "signup" ? signUp() : signIn())}
                  disabled={isAuthenticating}
                >
                  {isAuthenticating ? "Working..." : authMode === "signup" ? "Create Account" : "Sign In"}
                </button>
              </div>
            )}
          </div>
        </section>
      ) : null}

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
