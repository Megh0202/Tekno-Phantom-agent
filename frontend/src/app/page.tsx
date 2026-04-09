"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";

import {
  apiFetch as fetch,
  buildApiHeaders,
} from "@/lib/api-auth";
import styles from "./page.module.css";

type AgentConfig = {
  llm_mode: string;
  model: string;
  browser_mode?: string;
  filesystem_mode?: string;
  admin_auth_required?: boolean;
  jwt_auth_enabled?: boolean;
  max_steps_per_run: number;
};

type RuntimeStep = {
  step_id: string;
  index: number;
  type: string;
  input?: Record<string, unknown>;
  status: "pending" | "running" | "waiting_for_input" | "completed" | "failed" | "skipped" | "cancelled";
  message?: string | null;
  error?: string | null;
  user_input_kind?: string | null;
  user_input_prompt?: string | null;
};

type RunState = {
  run_id: string;
  run_name: string;
  status: "pending" | "running" | "waiting_for_input" | "completed" | "failed" | "cancelled";
  summary?: string | null;
  report_artifact?: string | null;
  steps: RuntimeStep[];
};

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
  created_at: string;
  updated_at: string;
};

type TestCaseSummary = {
  test_case_id: string;
  name: string;
  description?: string;
  prompt?: string;
  parent_folder_id?: string | null;
  start_url?: string | null;
  step_count: number;
  created_at: string;
  updated_at: string;
};

type FolderState = {
  folder_id: string;
  name: string;
  parent_folder_id?: string | null;
  created_at: string;
  updated_at: string;
};

type PlanGenerateResponse = {
  run_name: string;
  start_url?: string | null;
  steps: Record<string, unknown>[];
};

type StepImportResponse = {
  run_name: string;
  start_url?: string | null;
  steps: Record<string, unknown>[];
  source_filename: string;
  imported_count: number;
};

type JsonObject = Record<string, unknown>;

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

type SuiteRunState = {
  suite_run_id: string;
  suite_name: string;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  tests: Array<{
    test_case_id: string;
    name: string;
    status: "pending" | "running" | "completed" | "failed" | "cancelled";
    run_id?: string | null;
  }>;
  summary?: string | null;
  report_artifact?: string | null;
};

function resolveApiBaseUrl(): string {
  const configured = process.env.NEXT_PUBLIC_API_BASE_URL?.trim();
  if (typeof window === "undefined") {
    return configured || "http://localhost:8080";
  }

  const browserHost = window.location.hostname.trim();
  const browserProtocol = window.location.protocol === "https:" ? "https:" : "http:";
  if (!configured) {
    return `${browserProtocol}//${browserHost}:8080`;
  }

  try {
    const url = new URL(configured);
    const loopbackHosts = new Set(["localhost", "127.0.0.1"]);
    const configuredIsLoopback = loopbackHosts.has(url.hostname);
    const browserIsLoopback = loopbackHosts.has(browserHost);
    if (configuredIsLoopback !== browserIsLoopback) {
      url.hostname = browserHost;
      return url.toString().replace(/\/$/, "");
    }
    return configured.replace(/\/$/, "");
  } catch {
    return configured.replace(/\/$/, "");
  }
}

const API_BASE_URL = resolveApiBaseUrl();
const ADMIN_API_TOKEN = process.env.NEXT_PUBLIC_ADMIN_API_TOKEN?.trim() ?? "";
const DEFAULT_MAX_STEPS = 300;
const SHOW_ADVANCED_INPUTS =
  process.env.NEXT_PUBLIC_SHOW_ADVANCED_INPUTS?.trim().toLowerCase() === "true";

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
    // Ignore parse failures and use fallback message.
  }
  return `${response.status} ${response.statusText}`;
}

function toUserMessage(rawMessage: string): string {
  const lower = rawMessage.toLowerCase();
  if (
    lower.includes("invalid plan returned by brain") ||
    lower.includes("could not generate runnable steps") ||
    lower.includes("steps list should have at least 1 item")
  ) {
    return "Could not build runnable steps from that prompt. Add URL + clearer targets and try again.";
  }
  return rawMessage;
}

function isTerminal(status: RunState["status"] | undefined): boolean {
  return status === "completed" || status === "failed" || status === "cancelled";
}

function statusClass(status: RunState["status"] | RuntimeStep["status"] | undefined): string {
  if (status === "completed") return styles.statusCompleted;
  if (status === "failed") return styles.statusFailed;
  if (status === "running") return styles.statusRunning;
  if (status === "cancelled") return styles.statusCancelled;
  return styles.statusPending;
}

function firstFailedStep(run: RunState | null): RuntimeStep | null {
  if (!run) return null;
  return run.steps.find((step) => step.status === "failed" || step.status === "waiting_for_input") ?? null;
}

function formatStepType(stepType: string): string {
  return stepType
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
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
  const typeLabel = formatStepType(rawType);
  const details = Object.entries(step)
    .filter(([key]) => key !== "type")
    .map(([key, value]) => `${key}=${formatPlanValue(value)}`)
    .join(", ");
  return details ? `${typeLabel}: ${details}` : typeLabel;
}

function buildPromptFallbackFromSteps(steps: Record<string, unknown>[]): string {
  if (!steps.length) return "";
  return steps
    .map((step, index) => `${index + 1}. ${formatPlanStep(step)}`)
    .join("\n");
}

function extractUserStepLinesFromPrompt(prompt: string): string[] {
  return prompt
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => Boolean(line))
    .map((line) => line.replace(/^\d+[\).\s-]+/, "").trim());
}

function normalizeParentFolderId(value: string | null | undefined): string {
  return (value ?? "").trim();
}

function parseJsonObject(raw: string, label: string): JsonObject {
  const text = raw
    .replace(/\u2018|\u2019|\u2032/g, "'")
    .replace(/\u201c|\u201d|\u2033/g, '"')
    .trim();
  if (!text) return {};

  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error(`${label} must be valid JSON.`);
  }

  if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
    throw new Error(`${label} must be a JSON object.`);
  }
  return parsed as JsonObject;
}

function buildPlanSignature(prompt: string, testDataInput: string, selectorProfileInput: string): string {
  return [
    prompt.trim(),
    testDataInput.trim(),
    selectorProfileInput.trim(),
  ].join("||");
}

export default function Home() {
  const router = useRouter();
  const [config, setConfig] = useState<AgentConfig | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const [authUser, setAuthUser] = useState<AuthUser | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [authMode, setAuthMode] = useState<"signin" | "signup">("signin");
  const [authEmail, setAuthEmail] = useState("");
  const [authPassword, setAuthPassword] = useState("");
  const [authConfirmPassword, setAuthConfirmPassword] = useState("");
  const [isAuthenticating, setIsAuthenticating] = useState(false);

  const [prompt, setPrompt] = useState(
    "Open https://example.com, wait for full load, then verify h1 contains 'Example Domain'.",
  );
  const [testDataInput, setTestDataInput] = useState("{}");
  const [selectorProfileInput, setSelectorProfileInput] = useState("{}");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isCancelling, setIsCancelling] = useState(false);
  const [isGenerating, setIsGenerating] = useState(false);
  const [isSavingTestCase, setIsSavingTestCase] = useState(false);
  const [isImporting, setIsImporting] = useState(false);
  const [isRefreshingCases, setIsRefreshingCases] = useState(false);
  const [runningCaseId, setRunningCaseId] = useState<string | null>(null);
  const [currentRunSourceTestCaseId, setCurrentRunSourceTestCaseId] = useState<string | null>(null);
  const [selectorFixInputs, setSelectorFixInputs] = useState<Record<string, string>>({});
  const [selectorFixBusyByStepId, setSelectorFixBusyByStepId] = useState<Record<string, boolean>>({});
  const [requestError, setRequestError] = useState<string | null>(null);
  const [requestInfo, setRequestInfo] = useState<string | null>(null);
  const [planPreview, setPlanPreview] = useState<PlanGenerateResponse | null>(null);
  const [importedPlan, setImportedPlan] = useState<StepImportResponse | null>(null);
  const [importFile, setImportFile] = useState<File | null>(null);
  const [planSignature, setPlanSignature] = useState("");
  const [testCaseName, setTestCaseName] = useState("");
  const [testCaseDescription, setTestCaseDescription] = useState("");
  const [savedCases, setSavedCases] = useState<TestCaseSummary[]>([]);
  const [folders, setFolders] = useState<FolderState[]>([]);
  const [selectedFolderId, setSelectedFolderId] = useState<string | null>(null);
  const [expandedFolderIds, setExpandedFolderIds] = useState<Record<string, boolean>>({});
  const [expandedTestIds, setExpandedTestIds] = useState<Record<string, boolean>>({});
  const [folderNameInput, setFolderNameInput] = useState("");
  const [isCreatingFolder, setIsCreatingFolder] = useState(false);
  const testCaseNameInputRef = useRef<HTMLInputElement | null>(null);
  const [selectedTestCaseIds, setSelectedTestCaseIds] = useState<string[]>([]);
  const [selectedFolderIds, setSelectedFolderIds] = useState<string[]>([]);
  const [isStartingSuite, setIsStartingSuite] = useState(false);
  const [currentSuiteRun, setCurrentSuiteRun] = useState<SuiteRunState | null>(null);

  const [currentRun, setCurrentRun] = useState<RunState | null>(null);
  const requiresJwtAuth = Boolean(config?.jwt_auth_enabled) && !ADMIN_API_TOKEN;
  const authBlocked = requiresJwtAuth && (!authChecked || !authUser);

  const runIsActive = useMemo(
    () => currentRun && !isTerminal(currentRun.status),
    [currentRun],
  );
  const reportUrl = useMemo(() => {
    if (!currentRun?.run_id || !currentRun?.report_artifact) return null;
    return `${API_BASE_URL}/api/runs/${currentRun.run_id}/artifacts/report.html`;
  }, [currentRun]);
  const suiteReportUrl = useMemo(() => {
    if (!currentSuiteRun?.suite_run_id) return null;
    return `${API_BASE_URL}/api/suite-runs/${currentSuiteRun.suite_run_id}/artifacts/suite-report.html`;
  }, [currentSuiteRun]);
  const suiteIsActive = useMemo(() => {
    if (!currentSuiteRun) return false;
    return (
      currentSuiteRun.status === "pending" ||
      currentSuiteRun.status === "running"
    );
  }, [currentSuiteRun]);
  const planIsFresh = useMemo(() => {
    if (!planPreview) return false;
    const currentSignature = buildPlanSignature(prompt, testDataInput, selectorProfileInput);
    return planSignature === currentSignature;
  }, [planPreview, planSignature, prompt, selectorProfileInput, testDataInput]);
  const visiblePlan = useMemo<PlanGenerateResponse | null>(() => {
    if (importedPlan) {
      return {
        run_name: importedPlan.run_name,
        start_url: importedPlan.start_url ?? null,
        steps: importedPlan.steps,
      };
    }
    return SHOW_ADVANCED_INPUTS ? planPreview : null;
  }, [importedPlan, planPreview]);
  const selectedFolderName = useMemo(() => {
    if (!selectedFolderId) return "Root";
    const selected = folders.find((folder) => folder.folder_id === selectedFolderId);
    return selected?.name ?? "Root";
  }, [folders, selectedFolderId]);
  const folderChildrenMap = useMemo(() => {
    const map = new Map<string, FolderState[]>();
    for (const folder of folders) {
      const parentKey = normalizeParentFolderId(folder.parent_folder_id);
      const current = map.get(parentKey) ?? [];
      current.push(folder);
      map.set(parentKey, current);
    }
    for (const value of map.values()) {
      value.sort((a, b) => a.name.localeCompare(b.name));
    }
    return map;
  }, [folders]);
  const testsByFolderMap = useMemo(() => {
    const map = new Map<string, TestCaseSummary[]>();
    for (const testCase of savedCases) {
      const parentKey = normalizeParentFolderId(testCase.parent_folder_id);
      const current = map.get(parentKey) ?? [];
      current.push(testCase);
      map.set(parentKey, current);
    }
    for (const value of map.values()) {
      value.sort((a, b) => a.name.localeCompare(b.name));
    }
    return map;
  }, [savedCases]);
  const folderTestCountMap = useMemo(() => {
    const counts = new Map<string, number>();
    const visiting = new Set<string>();

    const countForFolder = (folderId: string): number => {
      if (counts.has(folderId)) return counts.get(folderId) ?? 0;
      if (visiting.has(folderId)) return 0;
      visiting.add(folderId);
      let total = (testsByFolderMap.get(folderId) ?? []).length;
      for (const child of folderChildrenMap.get(folderId) ?? []) {
        total += countForFolder(child.folder_id);
      }
      visiting.delete(folderId);
      counts.set(folderId, total);
      return total;
    };

    for (const folder of folders) {
      countForFolder(folder.folder_id);
    }
    return counts;
  }, [folderChildrenMap, folders, testsByFolderMap]);

  useEffect(() => {
    setExpandedFolderIds((previous) => {
      let changed = false;
      const next = { ...previous };
      for (const folder of folders) {
        if (next[folder.folder_id] === undefined) {
          next[folder.folder_id] = true;
          changed = true;
        }
      }
      return changed ? next : previous;
    });
  }, [folders]);

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
          setConfigError(null);
        }
      } catch (error) {
        if (!disposed) {
          setConfigError(error instanceof Error ? error.message : "Failed to load config");
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

  const loadTestCases = useCallback(async (options?: { silent?: boolean }): Promise<void> => {
    if (requiresJwtAuth && !authUser) {
      setSavedCases([]);
      return;
    }
    const silent = options?.silent ?? false;
    if (!silent) {
      setIsRefreshingCases(true);
    }
    try {
      const response = await fetch(`${API_BASE_URL}/api/test-cases`, {
        cache: "no-store",
        headers: buildApiHeaders({ adminToken: ADMIN_API_TOKEN }),
      });
      if (!response.ok) {
        throw new Error(await parseError(response));
      }
      const payload = (await response.json()) as { items: TestCaseSummary[] };
      setSavedCases(payload.items ?? []);
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "Failed to load test cases");
    } finally {
      if (!silent) {
        setIsRefreshingCases(false);
      }
    }
  }, [authUser, requiresJwtAuth]);

  const loadFolders = useCallback(async (options?: { silent?: boolean }): Promise<void> => {
    if (requiresJwtAuth && !authUser) {
      setFolders([]);
      return;
    }
    const silent = options?.silent ?? false;
    if (!silent) {
      setIsRefreshingCases(true);
    }
    try {
      const response = await fetch(`${API_BASE_URL}/api/test-folders`, {
        cache: "no-store",
        headers: buildApiHeaders({ adminToken: ADMIN_API_TOKEN }),
      });
      if (!response.ok) {
        throw new Error(await parseError(response));
      }
      const payload = (await response.json()) as { items: FolderState[] };
      setFolders(payload.items ?? []);
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "Failed to load folders");
    } finally {
      if (!silent) {
        setIsRefreshingCases(false);
      }
    }
  }, [authUser, requiresJwtAuth]);

  useEffect(() => {
    void loadTestCases({ silent: true });
  }, [loadTestCases]);

  useEffect(() => {
    void loadFolders({ silent: true });
  }, [loadFolders]);

  useEffect(() => {
    if (!currentRun) return;
    const shouldPoll = !isTerminal(currentRun.status) || !currentRun.report_artifact;
    if (!shouldPoll) return;

    let cancelled = false;
    const pollDelayMs = !isTerminal(currentRun.status) ? 350 : 900;

    const pollRun = async (): Promise<void> => {
      try {
        const response = await fetch(`${API_BASE_URL}/api/runs/${currentRun.run_id}`, {
          cache: "no-store",
          headers: buildApiHeaders({ adminToken: ADMIN_API_TOKEN }),
        });
        if (!response.ok) return;
        const payload = (await response.json()) as RunState;
        if (!cancelled) {
          setCurrentRun(payload);
        }
      } catch {
        // Poll errors are ignored while run is active.
      }
    };

    void pollRun();
    const interval = setInterval(() => {
      void pollRun();
    }, pollDelayMs);

    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [currentRun]);

  useEffect(() => {
    if (!currentSuiteRun) return;
    const terminal =
      currentSuiteRun.status === "completed" ||
      currentSuiteRun.status === "failed" ||
      currentSuiteRun.status === "cancelled";
    if (terminal) return;

    const interval = setInterval(async () => {
      try {
        const response = await fetch(`${API_BASE_URL}/api/suite-runs/${currentSuiteRun.suite_run_id}`, {
          cache: "no-store",
          headers: buildApiHeaders({ adminToken: ADMIN_API_TOKEN }),
        });
        if (!response.ok) return;
        const payload = (await response.json()) as SuiteRunState;
        setCurrentSuiteRun(payload);
      } catch {
        // Poll errors are ignored while suite run is active.
      }
    }, 1500);

    return () => clearInterval(interval);
  }, [currentSuiteRun]);

  async function requestPlan(
    task: string,
    testData: JsonObject,
    selectorProfile: JsonObject,
  ): Promise<PlanGenerateResponse> {
    const planResponse = await fetch(`${API_BASE_URL}/api/plan`, {
      method: "POST",
      headers: buildApiHeaders({ json: true, adminToken: ADMIN_API_TOKEN }),
      body: JSON.stringify({
        task,
        max_steps: config?.max_steps_per_run ?? DEFAULT_MAX_STEPS,
        test_data: testData,
        selector_profile: selectorProfile,
      }),
    });
    if (!planResponse.ok) {
      throw new Error(await parseError(planResponse));
    }

    const plan = (await planResponse.json()) as PlanGenerateResponse;
    if (!plan.steps || plan.steps.length === 0) {
      throw new Error("Planner returned no executable steps.");
    }
    return plan;
  }

  async function generatePlanPreview(): Promise<void> {
    setRequestError(null);
    setRequestInfo(null);

    const task = prompt.trim();
    if (!task) {
      setRequestError("Enter a prompt first.");
      return;
    }
    let testData: JsonObject;
    let selectorProfile: JsonObject;
    try {
      if (SHOW_ADVANCED_INPUTS) {
        testData = parseJsonObject(testDataInput, "Test Data");
        selectorProfile = parseJsonObject(selectorProfileInput, "Selector Profile");
      } else {
        testData = {};
        selectorProfile = {};
      }
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "Invalid JSON configuration");
      return;
    }

    try {
      setIsGenerating(true);
      setImportedPlan(null);
      const plan = await requestPlan(task, testData, selectorProfile);
      setPlanPreview(plan);
      setPlanSignature(buildPlanSignature(prompt, testDataInput, selectorProfileInput));
    } catch (error) {
      const rawMessage = error instanceof Error ? error.message : "Failed to generate plan";
      setRequestError(toUserMessage(rawMessage));
    } finally {
      setIsGenerating(false);
    }
  }

  async function importStepsFromFile(): Promise<void> {
    setRequestError(null);
    setRequestInfo(null);

    if (!importFile) {
      setRequestError("Choose a .csv or .xlsx file first.");
      return;
    }

    try {
      setIsImporting(true);
      const formData = new FormData();
      formData.append("file", importFile);
      const normalizedName = testCaseName.trim();
      if (normalizedName) {
        formData.append("run_name", normalizedName);
      }

      const response = await fetch(`${API_BASE_URL}/api/test-cases/import`, {
        method: "POST",
        headers: buildApiHeaders({ adminToken: ADMIN_API_TOKEN }),
        body: formData,
      });
      if (!response.ok) {
        throw new Error(await parseError(response));
      }

      const imported = (await response.json()) as StepImportResponse;
      setImportedPlan(imported);
      setPlanPreview({
        run_name: imported.run_name,
        start_url: imported.start_url ?? null,
        steps: imported.steps,
      });
      setPlanSignature("");
      if (!normalizedName) {
        setTestCaseName(imported.run_name);
      }
      setRequestInfo(
        `Imported ${imported.imported_count} steps from ${imported.source_filename}. Running now will use imported steps.`,
      );
    } catch (error) {
      const rawMessage = error instanceof Error ? error.message : "Failed to import steps file";
      setRequestError(toUserMessage(rawMessage));
    } finally {
      setIsImporting(false);
    }
  }

  function clearImportedPlan(): void {
    setImportedPlan(null);
    setRequestInfo("Imported steps cleared. Prompt planning mode is active.");
  }

  async function createFolder(): Promise<void> {
    setRequestError(null);
    setRequestInfo(null);

    const normalizedName = folderNameInput.trim();
    if (!normalizedName) {
      setRequestError("Enter folder name before creating.");
      return;
    }

    try {
      setIsCreatingFolder(true);
      const response = await fetch(`${API_BASE_URL}/api/test-folders`, {
        method: "POST",
        headers: buildApiHeaders({ json: true, adminToken: ADMIN_API_TOKEN }),
        body: JSON.stringify({
          name: normalizedName,
          parent_folder_id: selectedFolderId,
        }),
      });
      if (!response.ok) {
        throw new Error(await parseError(response));
      }
      const created = (await response.json()) as FolderState;
      setFolderNameInput("");
      setSelectedFolderId(created.folder_id);
      setRequestInfo(`Created folder: ${created.name}`);
      await loadFolders({ silent: true });
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "Failed to create folder");
    } finally {
      setIsCreatingFolder(false);
    }
  }

  async function deleteFolder(folderId: string, folderName: string): Promise<void> {
    const confirmed = window.confirm(
      `Delete folder "${folderName}" and all nested subfolders and test cases?`,
    );
    if (!confirmed) return;

    setRequestError(null);
    setRequestInfo(null);
    try {
      const response = await fetch(`${API_BASE_URL}/api/test-folders/${folderId}`, {
        method: "DELETE",
        headers: buildApiHeaders({ adminToken: ADMIN_API_TOKEN }),
      });
      if (!response.ok) {
        if (response.status === 404) {
          throw new Error("Delete API not available on backend. Please restart backend server.");
        }
        throw new Error(await parseError(response));
      }
      if (selectedFolderId === folderId) {
        setSelectedFolderId(null);
      }
      setSelectedFolderIds((previous) => previous.filter((item) => item !== folderId));
      setRequestInfo(`Deleted folder: ${folderName}`);
      await loadFolders({ silent: true });
      await loadTestCases({ silent: true });
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "Failed to delete folder");
    }
  }

  async function deleteTestCase(testCaseId: string, testCaseNameValue: string): Promise<void> {
    const confirmed = window.confirm(`Delete test case "${testCaseNameValue}"?`);
    if (!confirmed) return;

    setRequestError(null);
    setRequestInfo(null);
    try {
      const response = await fetch(`${API_BASE_URL}/api/test-cases/${testCaseId}`, {
        method: "DELETE",
        headers: buildApiHeaders({ adminToken: ADMIN_API_TOKEN }),
      });
      if (!response.ok) {
        if (response.status === 404) {
          throw new Error("Delete API not available on backend. Please restart backend server.");
        }
        throw new Error(await parseError(response));
      }
      setRequestInfo(`Deleted test case: ${testCaseNameValue}`);
      await loadTestCases({ silent: true });
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "Failed to delete test case");
    }
  }

  function toggleTestCaseSelection(testCaseId: string): void {
    setSelectedTestCaseIds((previous) => {
      if (previous.includes(testCaseId)) {
        return previous.filter((item) => item !== testCaseId);
      }
      return [...previous, testCaseId];
    });
  }

  function toggleFolderSelection(folderId: string): void {
    setSelectedFolderIds((previous) => {
      if (previous.includes(folderId)) {
        return previous.filter((item) => item !== folderId);
      }
      return [...previous, folderId];
    });
  }

  async function runSelectedTestsInParallel(): Promise<void> {
    if (selectedTestCaseIds.length < 2) {
      setRequestError("Select at least two tests to run in parallel.");
      return;
    }
    setRequestError(null);
    setRequestInfo(null);
    try {
      setIsStartingSuite(true);
      const response = await fetch(`${API_BASE_URL}/api/suite-runs`, {
        method: "POST",
        headers: buildApiHeaders({ json: true, adminToken: ADMIN_API_TOKEN }),
        body: JSON.stringify({
          suite_name: "selected-tests-suite",
          test_case_ids: selectedTestCaseIds,
          max_parallel: Math.min(selectedTestCaseIds.length, 4),
        }),
      });
      if (!response.ok) {
        throw new Error(await parseError(response));
      }
      const suite = (await response.json()) as SuiteRunState;
      setCurrentSuiteRun(suite);
      setRequestInfo(`Parallel suite started: ${suite.suite_run_id}`);
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "Failed to start parallel suite");
    } finally {
      setIsStartingSuite(false);
    }
  }

  async function runCurrentFolderInParallel(): Promise<void> {
    const folderScopeIds = selectedFolderIds.length > 0 ? selectedFolderIds : (selectedFolderId ? [selectedFolderId] : []);
    if (folderScopeIds.length === 0) {
      setRequestError("Select one or more folders first to run folder tests in parallel.");
      return;
    }

    const scopedTestIds = new Set<string>();
    const stack = [...folderScopeIds];
    const visited = new Set<string>();
    while (stack.length > 0) {
      const currentFolderId = stack.pop() as string;
      if (visited.has(currentFolderId)) continue;
      visited.add(currentFolderId);

      for (const testCase of testsByFolderMap.get(currentFolderId) ?? []) {
        scopedTestIds.add(testCase.test_case_id);
      }
      for (const childFolder of folderChildrenMap.get(currentFolderId) ?? []) {
        stack.push(childFolder.folder_id);
      }
    }

    if (scopedTestIds.size === 0) {
      setRequestError("No test cases found under selected folder scope.");
      return;
    }

    setRequestError(null);
    setRequestInfo(null);
    try {
      setIsStartingSuite(true);
      const response = await fetch(`${API_BASE_URL}/api/suite-runs`, {
        method: "POST",
        headers: buildApiHeaders({ json: true, adminToken: ADMIN_API_TOKEN }),
        body: JSON.stringify({
          suite_name: folderScopeIds.length > 1 ? "multi-folder-suite-run" : "folder-suite-run",
          test_case_ids: Array.from(scopedTestIds),
          max_parallel: Math.min(scopedTestIds.size, 4),
        }),
      });
      if (!response.ok) {
        throw new Error(await parseError(response));
      }
      const suite = (await response.json()) as SuiteRunState;
      setCurrentSuiteRun(suite);
      setRequestInfo(`Folder parallel suite started: ${suite.suite_run_id} (${scopedTestIds.size} tests)`);
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "Failed to start folder suite");
    } finally {
      setIsStartingSuite(false);
    }
  }

  async function runFromPrompt(event: React.FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setRequestError(null);
    setRequestInfo(null);

    const task = prompt.trim();
    if (!task && !importedPlan) {
      setRequestError("Enter a prompt first.");
      return;
    }
    let testData: JsonObject;
    let selectorProfile: JsonObject;
    try {
      if (SHOW_ADVANCED_INPUTS) {
        testData = parseJsonObject(testDataInput, "Test Data");
        selectorProfile = parseJsonObject(selectorProfileInput, "Selector Profile");
      } else {
        testData = {};
        selectorProfile = {};
      }
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "Invalid JSON configuration");
      return;
    }

    try {
      setIsSubmitting(true);

      let plan: PlanGenerateResponse;
      if (importedPlan) {
        plan = {
          run_name: importedPlan.run_name,
          start_url: importedPlan.start_url ?? null,
          steps: importedPlan.steps,
        };
      } else {
        const useCachedPlan = SHOW_ADVANCED_INPUTS && Boolean(planIsFresh && planPreview);
        plan = useCachedPlan && planPreview ? planPreview : await requestPlan(task, testData, selectorProfile);
        if (!useCachedPlan) {
          setPlanPreview(plan);
          setPlanSignature(buildPlanSignature(prompt, testDataInput, selectorProfileInput));
        }
      }

      const runResponse = await fetch(`${API_BASE_URL}/api/runs`, {
        method: "POST",
        headers: buildApiHeaders({ json: true, adminToken: ADMIN_API_TOKEN }),
        body: JSON.stringify({
          run_name: plan.run_name || "prompt-run",
          start_url: plan.start_url ?? null,
          steps: plan.steps,
          test_data: testData,
          selector_profile: selectorProfile,
        }),
      });
      if (!runResponse.ok) {
        throw new Error(await parseError(runResponse));
      }

      const run = (await runResponse.json()) as RunState;
      setCurrentRun(run);
      setCurrentRunSourceTestCaseId(null);
      setRequestInfo(`Run started: ${run.run_id}`);
    } catch (error) {
      const rawMessage = error instanceof Error ? error.message : "Failed to execute prompt";
      setRequestError(toUserMessage(rawMessage));
    } finally {
      setIsSubmitting(false);
    }
  }

  async function saveTestCaseFromPrompt(): Promise<void> {
    setRequestError(null);
    setRequestInfo(null);

    const task = prompt.trim();
    if (!task && !importedPlan) {
      setRequestError("Enter a prompt first.");
      return;
    }

    const normalizedName = testCaseName.trim();
    if (!normalizedName) {
      setRequestError("Enter test case name before saving.");
      return;
    }

    let testData: JsonObject;
    let selectorProfile: JsonObject;
    try {
      if (SHOW_ADVANCED_INPUTS) {
        testData = parseJsonObject(testDataInput, "Test Data");
        selectorProfile = parseJsonObject(selectorProfileInput, "Selector Profile");
      } else {
        testData = {};
        selectorProfile = {};
      }
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "Invalid JSON configuration");
      return;
    }

    try {
      setIsSavingTestCase(true);
      let plan: PlanGenerateResponse;
      if (importedPlan) {
        plan = {
          run_name: importedPlan.run_name,
          start_url: importedPlan.start_url ?? null,
          steps: importedPlan.steps,
        };
      } else {
        const useCachedPlan = SHOW_ADVANCED_INPUTS && Boolean(planIsFresh && planPreview);
        plan = useCachedPlan && planPreview ? planPreview : await requestPlan(task, testData, selectorProfile);
        if (!useCachedPlan) {
          setPlanPreview(plan);
          setPlanSignature(buildPlanSignature(prompt, testDataInput, selectorProfileInput));
        }
      }

      const response = await fetch(`${API_BASE_URL}/api/test-cases`, {
        method: "POST",
        headers: buildApiHeaders({ json: true, adminToken: ADMIN_API_TOKEN }),
        body: JSON.stringify({
          name: normalizedName,
          description: testCaseDescription.trim(),
          prompt: task || buildPromptFallbackFromSteps(plan.steps),
          parent_folder_id: selectedFolderId,
          start_url: plan.start_url ?? null,
          steps: plan.steps,
          test_data: testData,
          selector_profile: selectorProfile,
        }),
      });
      if (!response.ok) {
        throw new Error(await parseError(response));
      }
      const saved = (await response.json()) as TestCaseState;
      setRequestInfo(`Saved test case: ${saved.name}`);
      await loadTestCases({ silent: true });
    } catch (error) {
      const rawMessage = error instanceof Error ? error.message : "Failed to save test case";
      setRequestError(toUserMessage(rawMessage));
    } finally {
      setIsSavingTestCase(false);
    }
  }

  async function runSavedTestCase(testCaseId: string): Promise<void> {
    setRequestError(null);
    setRequestInfo(null);
    try {
      setRunningCaseId(testCaseId);
      const detailResponse = await fetch(`${API_BASE_URL}/api/test-cases/${testCaseId}`, {
        cache: "no-store",
        headers: buildApiHeaders({ adminToken: ADMIN_API_TOKEN }),
      });
      if (detailResponse.ok) {
        const detail = (await detailResponse.json()) as TestCaseState;
        const savedPrompt = (detail.prompt ?? "").trim();
        const fallbackPrompt = buildPromptFallbackFromSteps(detail.steps ?? []);
        const promptStepLines = extractUserStepLinesFromPrompt(savedPrompt);
        setPrompt(savedPrompt || fallbackPrompt);
        setTestCaseName(detail.name ?? "");
        setTestCaseDescription(detail.description ?? "");
        if (!savedPrompt && promptStepLines.length > 0) {
          setPrompt(promptStepLines.join("\n"));
        }
        setSelectedFolderId(detail.parent_folder_id ?? null);
      }

      if (
        currentRun &&
        currentRunSourceTestCaseId === testCaseId &&
        (currentRun.status === "failed" || currentRun.status === "waiting_for_input") &&
        firstFailedStep(currentRun)
      ) {
        const response = await fetch(`${API_BASE_URL}/api/runs/${currentRun.run_id}/resume`, {
          method: "POST",
          headers: buildApiHeaders({ json: true, adminToken: ADMIN_API_TOKEN }),
          body: JSON.stringify({
            run_name: `${currentRun.run_name} [resume]`,
          }),
        });
        if (!response.ok) {
          throw new Error(await parseError(response));
        }
        const resumed = (await response.json()) as RunState;
        setCurrentRun(resumed);
        setRequestInfo(`Resumed from failed step: ${resumed.run_name}`);
        return;
      }

      const response = await fetch(`${API_BASE_URL}/api/test-cases/${testCaseId}/run`, {
        method: "POST",
        headers: buildApiHeaders({ adminToken: ADMIN_API_TOKEN }),
      });
      if (!response.ok) {
        throw new Error(await parseError(response));
      }
      const run = (await response.json()) as RunState;
      setCurrentRun(run);
      setCurrentRunSourceTestCaseId(testCaseId);
      setRequestInfo(`Run started from saved test case: ${run.run_name}`);
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "Failed to run saved test case");
    } finally {
      setRunningCaseId(null);
    }
  }

  async function cancelRun(): Promise<void> {
    if (!currentRun || isTerminal(currentRun.status)) return;

    try {
      setIsCancelling(true);
      const response = await fetch(`${API_BASE_URL}/api/runs/${currentRun.run_id}/cancel`, {
        method: "POST",
        headers: buildApiHeaders({ adminToken: ADMIN_API_TOKEN }),
      });
      if (!response.ok) {
        throw new Error(await parseError(response));
      }

      const refreshed = await fetch(`${API_BASE_URL}/api/runs/${currentRun.run_id}`, {
        cache: "no-store",
        headers: buildApiHeaders({ adminToken: ADMIN_API_TOKEN }),
      });
      if (refreshed.ok) {
        const payload = (await refreshed.json()) as RunState;
        setCurrentRun(payload);
      }
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "Failed to cancel run");
    } finally {
      setIsCancelling(false);
    }
  }

  function editableSelectorField(step: RuntimeStep): "selector" | "source_selector" | "target_selector" | null {
    const payload = step.input ?? {};
    if (typeof payload.selector === "string" && payload.selector.trim()) return "selector";
    if (typeof payload.source_selector === "string" && payload.source_selector.trim()) return "source_selector";
    if (typeof payload.target_selector === "string" && payload.target_selector.trim()) return "target_selector";
    return null;
  }

  function defaultSelectorForStep(step: RuntimeStep): string {
    const field = editableSelectorField(step);
    if (!field) return "";
    const payload = step.input ?? {};
    const value = payload[field];
    return typeof value === "string" ? value : "";
  }

  async function applySelectorFixAndRerun(step: RuntimeStep): Promise<void> {
    if (!currentRun) {
      setRequestError("No active run context found for selector recovery.");
      return;
    }

    const stepField = editableSelectorField(step);
    if (!stepField) {
      setRequestError("This failed step does not expose an editable selector field.");
      return;
    }

    const selectorValue = (selectorFixInputs[step.step_id] ?? defaultSelectorForStep(step)).trim();
    if (!selectorValue) {
      setRequestError("Paste a valid selector before applying recovery.");
      return;
    }

    setRequestError(null);
    setRequestInfo(null);
    setSelectorFixBusyByStepId((previous) => ({ ...previous, [step.step_id]: true }));
    try {
      if (currentRunSourceTestCaseId) {
        const detailResponse = await fetch(`${API_BASE_URL}/api/test-cases/${currentRunSourceTestCaseId}`, {
          cache: "no-store",
          headers: buildApiHeaders({ adminToken: ADMIN_API_TOKEN }),
        });
        if (!detailResponse.ok) {
          throw new Error(await parseError(detailResponse));
        }
        const detail = (await detailResponse.json()) as TestCaseState;
        const steps = [...(detail.steps ?? [])];

        const directCandidate = steps[step.index] as Record<string, unknown> | undefined;
        let stepIndexToPatch = -1;
        if (directCandidate && directCandidate.type === step.type) {
          stepIndexToPatch = step.index;
        } else {
          const failedSelector = defaultSelectorForStep(step);
          stepIndexToPatch = steps.findIndex((candidate) => {
            const row = candidate as Record<string, unknown>;
            if (row.type !== step.type) return false;
            const candidateValue = row[stepField];
            return typeof candidateValue === "string" && candidateValue === failedSelector;
          });
        }

        if (stepIndexToPatch < 0 || stepIndexToPatch >= steps.length) {
          throw new Error("Could not map failed runtime step to its saved test step.");
        }

        const updatedStep = { ...(steps[stepIndexToPatch] as Record<string, unknown>) };
        updatedStep[stepField] = selectorValue;
        steps[stepIndexToPatch] = updatedStep;

        const updateResponse = await fetch(`${API_BASE_URL}/api/test-cases/${currentRunSourceTestCaseId}`, {
          method: "PUT",
          headers: buildApiHeaders({ json: true, adminToken: ADMIN_API_TOKEN }),
          body: JSON.stringify({
            name: detail.name,
            description: detail.description ?? "",
            prompt: detail.prompt ?? "",
            parent_folder_id: detail.parent_folder_id ?? null,
            start_url: detail.start_url ?? null,
            steps,
            test_data: detail.test_data ?? {},
            selector_profile: detail.selector_profile ?? {},
          }),
        });
        if (!updateResponse.ok) {
          throw new Error(await parseError(updateResponse));
        }

        const rerunResponse = await fetch(`${API_BASE_URL}/api/runs/${currentRun.run_id}/steps/${step.step_id}/selector`, {
          method: "POST",
          headers: buildApiHeaders({ json: true, adminToken: ADMIN_API_TOKEN }),
          body: JSON.stringify({
            selector: selectorValue,
          }),
        });
        if (!rerunResponse.ok) {
          throw new Error(await parseError(rerunResponse));
        }
        const run = (await rerunResponse.json()) as RunState;
        setCurrentRun(run);
        setRequestInfo(
          `Selector updated in saved test case. Continuing step #${step.index + 1} in run ${run.run_id}.`,
        );
        await loadTestCases({ silent: true });
      } else {
        const response = await fetch(`${API_BASE_URL}/api/runs/${currentRun.run_id}/steps/${step.step_id}/selector`, {
          method: "POST",
          headers: buildApiHeaders({ json: true, adminToken: ADMIN_API_TOKEN }),
          body: JSON.stringify({
            selector: selectorValue,
          }),
        });
        if (!response.ok) {
          throw new Error(await parseError(response));
        }
        const run = (await response.json()) as RunState;
        setCurrentRun(run);
        setCurrentRunSourceTestCaseId(null);
        setRequestInfo(
          `Selector updated. Continuing step #${step.index + 1} in run ${run.run_id}.`,
        );
      }
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "Failed to apply selector fix");
    } finally {
      setSelectorFixBusyByStepId((previous) => ({ ...previous, [step.step_id]: false }));
    }
  }

  function toggleFolderExpanded(folderId: string): void {
    setExpandedFolderIds((previous) => ({
      ...previous,
      [folderId]: !previous[folderId],
    }));
  }

  function startCreateTestCaseInFolder(folderId: string | null, folderName: string): void {
    setSelectedFolderId(folderId);
    setRequestError(null);
    setRequestInfo(`Creating new test case in "${folderName}". Enter details above and click Save Test Case.`);
    testCaseNameInputRef.current?.focus();
    testCaseNameInputRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function toggleTestExpanded(testCaseId: string): void {
    setExpandedTestIds((previous) => ({
      ...previous,
      [testCaseId]: !previous[testCaseId],
    }));
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
      await loadFolders({ silent: true });
      await loadTestCases({ silent: true });
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
      await loadFolders({ silent: true });
      await loadTestCases({ silent: true });
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
    setSavedCases([]);
    setFolders([]);
    setRequestError(null);
    setRequestInfo("Signed out. Sign back in to plan, save, or run tests.");
  }

  function renderLibraryNodes(parentFolderId: string | null, depth: number): ReactNode {
    const parentKey = normalizeParentFolderId(parentFolderId);
    const childFolders = folderChildrenMap.get(parentKey) ?? [];
    const childTests = testsByFolderMap.get(parentKey) ?? [];

    return (
      <>
        {childFolders.map((folder) => {
          const isSelected = selectedFolderId === folder.folder_id;
          const isExpanded = expandedFolderIds[folder.folder_id] ?? true;
          const hasChildren =
            (folderChildrenMap.get(folder.folder_id)?.length ?? 0) > 0 ||
            (testsByFolderMap.get(folder.folder_id)?.length ?? 0) > 0;
          const folderCount = folderTestCountMap.get(folder.folder_id) ?? 0;
          return (
            <div key={folder.folder_id} className={styles.treeRow} style={{ marginLeft: `${depth * 16}px` }}>
              <div className={styles.treeNodeRow}>
                <button
                  type="button"
                  className={styles.expandButton}
                  onClick={() => toggleFolderExpanded(folder.folder_id)}
                  disabled={!hasChildren}
                  aria-label={isExpanded ? "Collapse folder" : "Expand folder"}
                >
                  {hasChildren ? (isExpanded ? "▾" : "▸") : "·"}
                </button>
                <input
                  type="checkbox"
                  className={styles.folderSelectCheckbox}
                  checked={selectedFolderIds.includes(folder.folder_id)}
                  onChange={() => toggleFolderSelection(folder.folder_id)}
                  title="Select folder for folder-parallel run"
                  aria-label={`Select folder ${folder.name} for folder-parallel run`}
                />
                <button
                  type="button"
                  className={`${styles.folderRowButton} ${isSelected ? styles.folderRowButtonActive : ""}`}
                  onClick={() => setSelectedFolderId(folder.folder_id)}
                >
                  {folder.name}
                </button>
                <span className={styles.treeCount}>{folderCount} tests</span>
                <button
                  type="button"
                  className={styles.addIconButton}
                  onClick={() => startCreateTestCaseInFolder(folder.folder_id, folder.name)}
                  title="Create test case in this folder"
                  aria-label={`Create test case in folder ${folder.name}`}
                >
                  +
                </button>
                <button
                  type="button"
                  className={styles.deleteIconButton}
                  onClick={() => void deleteFolder(folder.folder_id, folder.name)}
                  title="Delete folder"
                  aria-label={`Delete folder ${folder.name}`}
                >
                  Del
                </button>
              </div>
              {isExpanded ? renderLibraryNodes(folder.folder_id, depth + 1) : null}
            </div>
          );
        })}
        {childTests.map((testCase) => (
          <article key={testCase.test_case_id} className={styles.testNode} style={{ marginLeft: `${depth * 16}px` }}>
            <div className={styles.treeNodeRowTest}>
              <input
                type="checkbox"
                checked={selectedTestCaseIds.includes(testCase.test_case_id)}
                onChange={() => toggleTestCaseSelection(testCase.test_case_id)}
              />
              <button
                type="button"
                className={styles.expandButton}
                onClick={() => toggleTestExpanded(testCase.test_case_id)}
                aria-label={expandedTestIds[testCase.test_case_id] ? "Collapse test steps" : "Expand test steps"}
              >
                {expandedTestIds[testCase.test_case_id] ? "▾" : "▸"}
              </button>
              <button
                type="button"
                className={styles.testRowButton}
                onClick={() => router.push(`/test-cases/${testCase.test_case_id}`)}
              >
                {testCase.name}
              </button>
              <span className={styles.treeCount}>{testCase.step_count} steps</span>
              <button
                type="button"
                className={styles.secondaryButton}
                onClick={() => void runSavedTestCase(testCase.test_case_id)}
                disabled={authBlocked || Boolean(runningCaseId)}
              >
                {runningCaseId === testCase.test_case_id ? "Starting..." : "Run"}
              </button>
              <button
                type="button"
                className={styles.deleteIconButton}
                onClick={() => void deleteTestCase(testCase.test_case_id, testCase.name)}
                title="Delete test case"
                aria-label={`Delete test case ${testCase.name}`}
              >
                Del
              </button>
            </div>
            {expandedTestIds[testCase.test_case_id] ? (
              <div className={styles.testStepsBox}>
                {extractUserStepLinesFromPrompt((testCase.prompt ?? "").trim()).length > 0 ? (
                  <ol className={styles.testStepList}>
                    {extractUserStepLinesFromPrompt((testCase.prompt ?? "").trim()).map((line, index) => (
                      <li key={`${testCase.test_case_id}-line-${index}`} className={styles.testStepItem}>
                        {line}
                      </li>
                    ))}
                  </ol>
                ) : (
                  <p className={styles.metaLine}>No user-entered steps found for this test case.</p>
                )}
              </div>
            ) : null}
          </article>
        ))}
      </>
    );
  }

  return (
    <div className={styles.shell}>
      <header className={styles.hero}>
        <div className={styles.heroLeft}>
          <p className={styles.kicker}>Tekno Phantom</p>
          <h1>Tekno Phantom</h1>
          <p className={styles.subtitle}>
            Prompt in, result out. Describe your browser task in plain language and Tekno Phantom
            will plan and execute it automatically.
          </p>
        </div>
        <div className={styles.heroRight}>
          <div className={styles.heroCard}>
            <p className={styles.heroCardTitle}>Live Config</p>
            {configError ? (
              <p className={styles.errorText}>{configError}</p>
            ) : (
              <p className={styles.metaLine}>
                {config?.llm_mode ?? "loading"} · {config?.model ?? "loading"} ·{" "}
                {config?.browser_mode ?? "loading"} · {config?.filesystem_mode ?? "loading"}
              </p>
            )}
          </div>
          <div className={styles.heroCard}>
            <p className={styles.heroCardTitle}>Access</p>
            {requiresJwtAuth ? (
              authUser ? (
                <div className={styles.authBox}>
                  <p className={styles.metaLine}>
                    Signed in as {authUser.email} ({authUser.role}).
                  </p>
                  <div className={styles.authActions}>
                    <button type="button" className={styles.secondaryButton} onClick={() => void signOut()}>
                      Sign Out
                    </button>
                  </div>
                </div>
              ) : (
                <div className={styles.authBox}>
                  <p className={styles.metaLine}>
                    {authMode === "signup"
                      ? "Create your account here. Registration also signs you in."
                      : "Sign in to plan, save, import, and run test cases."}
                  </p>
                  <div className={styles.authActions}>
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
                  </div>
                  <div className={styles.authGrid}>
                    <label className={styles.fieldLabel}>
                      <span>Email</span>
                      <input
                        value={authEmail}
                        onChange={(event) => setAuthEmail(event.target.value)}
                        placeholder="qa@example.com"
                      />
                    </label>
                    <label className={styles.fieldLabel}>
                      <span>Password</span>
                      <input
                        type="password"
                        value={authPassword}
                        onChange={(event) => setAuthPassword(event.target.value)}
                        placeholder="Enter password"
                      />
                    </label>
                    {authMode === "signup" ? (
                      <label className={styles.fieldLabel}>
                        <span>Confirm Password</span>
                        <input
                          type="password"
                          value={authConfirmPassword}
                          onChange={(event) => setAuthConfirmPassword(event.target.value)}
                          placeholder="Confirm password"
                        />
                      </label>
                    ) : null}
                  </div>
                  <div className={styles.authActions}>
                    <button
                      type="button"
                      className={styles.primaryButton}
                      onClick={() => void (authMode === "signup" ? signUp() : signIn())}
                      disabled={isAuthenticating}
                    >
                      {isAuthenticating ? "Working..." : authMode === "signup" ? "Create Account" : "Sign In"}
                    </button>
                  </div>
                </div>
              )
            ) : (
              <p className={styles.metaLine}>
                {ADMIN_API_TOKEN ? "Using frontend admin token." : "Planner access is open."}
              </p>
            )}
          </div>
          <div className={styles.heroCardMuted}>
            <p>Natural-language browser automation with save + rerun support.</p>
          </div>
        </div>
      </header>

      <main className={styles.workspace}>
        <div className={styles.primaryColumn}>
          <section className={styles.panel}>
            <h2>Automation Prompt</h2>
            <form onSubmit={runFromPrompt} className={styles.form}>
              <label className={styles.fieldLabel}>
                <span>Prompt</span>
                <textarea
                  rows={4}
                  value={prompt}
                  onChange={(event) => setPrompt(event.target.value)}
                  placeholder="Example: Open https://example.com and verify h1 contains Example Domain."
                />
              </label>

              <div className={styles.fieldSplit}>
                <label className={styles.fieldLabel}>
                  <span>Test Case Name</span>
                  <input
                    ref={testCaseNameInputRef}
                    value={testCaseName}
                    onChange={(event) => setTestCaseName(event.target.value)}
                    placeholder="Create_Form_01"
                  />
                </label>

                <label className={styles.fieldLabel}>
                  <span>Description</span>
                  <textarea
                    rows={2}
                    value={testCaseDescription}
                    onChange={(event) => setTestCaseDescription(event.target.value)}
                    placeholder="Create form flow with required field verification."
                  />
                </label>
              </div>

              <div className={styles.importBlock}>
                <label className={styles.fieldLabel}>
                  <span>Import Steps File (.csv / .xlsx)</span>
                  <input
                    className={styles.fileInput}
                    type="file"
                    accept=".csv,.xlsx"
                    onChange={(event) => {
                      const selected = event.target.files?.[0] ?? null;
                      setImportFile(selected);
                    }}
                  />
                </label>
                <div className={styles.importActions}>
                  <button
                    type="button"
                    className={styles.secondaryButton}
                    onClick={importStepsFromFile}
                    disabled={authBlocked || !importFile || isImporting || isSubmitting || isSavingTestCase}
                  >
                    {isImporting ? "Importing..." : "Import Steps"}
                  </button>
                  {importedPlan ? (
                    <button
                      type="button"
                      className={styles.secondaryButton}
                      onClick={clearImportedPlan}
                      disabled={isSubmitting || isSavingTestCase}
                    >
                      Clear Imported
                    </button>
                  ) : null}
                </div>
              </div>

              {SHOW_ADVANCED_INPUTS ? (
                <>
                  <label className={styles.fieldLabel}>
                    <span>Test Data (JSON)</span>
                    <textarea
                      rows={5}
                      value={testDataInput}
                      onChange={(event) => setTestDataInput(event.target.value)}
                      placeholder='{"email":"qa@example.com","password":"secret123"}'
                    />
                  </label>

                  <label className={styles.fieldLabel}>
                    <span>Selector Profile (JSON)</span>
                    <textarea
                      rows={5}
                      value={selectorProfileInput}
                      onChange={(event) => setSelectorProfileInput(event.target.value)}
                      placeholder='{"email":["#username"],"password":["#password"]}'
                    />
                  </label>
                </>
              ) : null}

              {requestError ? <p className={styles.errorText}>{requestError}</p> : null}
              {requestInfo ? <p className={styles.infoText}>{requestInfo}</p> : null}

              <div className={styles.actions}>
                {SHOW_ADVANCED_INPUTS ? (
                  <button
                    type="button"
                    className={styles.secondaryButton}
                    onClick={generatePlanPreview}
                    disabled={authBlocked || isGenerating || isSubmitting}
                  >
                    {isGenerating ? "Generating..." : "Generate Steps (AI)"}
                  </button>
                ) : null}
                <button
                  type="button"
                  className={styles.secondaryButton}
                  onClick={saveTestCaseFromPrompt}
                  disabled={authBlocked || isSavingTestCase || isSubmitting || isImporting}
                >
                  {isSavingTestCase ? "Saving..." : "Save Test Case"}
                </button>
                <button
                  type="submit"
                  className={styles.primaryButton}
                  disabled={authBlocked || isSubmitting || isImporting}
                >
                  {isSubmitting ? "Starting..." : "Run Prompt"}
                </button>
                <button
                  type="button"
                  className={styles.secondaryButton}
                  onClick={cancelRun}
                  disabled={authBlocked || !runIsActive || isCancelling}
                >
                  {isCancelling ? "Cancelling..." : "Cancel"}
                </button>
              </div>

              {visiblePlan ? (
                <div className={styles.planPreview}>
                  <div className={styles.planHeader}>
                    <h3>
                      {importedPlan ? "Imported Steps" : "Generated Steps"} ({visiblePlan.steps.length})
                    </h3>
                    {!importedPlan && SHOW_ADVANCED_INPUTS && !planIsFresh ? (
                      <p className={styles.planStale}>Prompt changed. Generate steps again before run.</p>
                    ) : null}
                  </div>
                  {importedPlan ? (
                    <p className={styles.metaLine}>Source: {importedPlan.source_filename}</p>
                  ) : null}
                  <p className={styles.metaLine}>
                    Run Name: {visiblePlan.run_name}
                    {visiblePlan.start_url ? ` | Start URL: ${visiblePlan.start_url}` : ""}
                  </p>
                  <ol className={styles.planList}>
                    {visiblePlan.steps.map((step, index) => (
                      <li key={`plan-step-${index}`} className={styles.planItem}>
                        {formatPlanStep(step)}
                      </li>
                    ))}
                  </ol>
                </div>
              ) : null}
            </form>
          </section>

          <section className={styles.panel}>
            <div className={styles.savedHeader}>
              <h2>Saved Test Cases</h2>
              <div className={styles.savedActions}>
                <button
                  type="button"
                  className={styles.secondaryButton}
                  onClick={() => {
                    void loadFolders();
                    void loadTestCases();
                  }}
                  disabled={authBlocked || isRefreshingCases}
                >
                  {isRefreshingCases ? "Refreshing..." : "Refresh"}
                </button>
                <button
                  type="button"
                  className={styles.secondaryButton}
                  onClick={() => void runSelectedTestsInParallel()}
                  disabled={authBlocked || isStartingSuite || selectedTestCaseIds.length < 2}
                >
                  {isStartingSuite ? "Starting..." : "Run Selected Parallel"}
                </button>
                <button
                  type="button"
                  className={styles.secondaryButton}
                  onClick={() => void runCurrentFolderInParallel()}
                  disabled={authBlocked || isStartingSuite || (selectedFolderIds.length === 0 && !selectedFolderId)}
                >
                  {isStartingSuite ? "Starting..." : "Run Folder Parallel"}
                </button>
              </div>
            </div>

            <div className={styles.folderCreateRow}>
              <input
                value={folderNameInput}
                onChange={(event) => setFolderNameInput(event.target.value)}
                placeholder="Folder name"
              />
              <button
                type="button"
                className={styles.secondaryButton}
                onClick={() => void createFolder()}
                disabled={authBlocked || isCreatingFolder}
              >
                {isCreatingFolder ? "Creating..." : "+ Folder"}
              </button>
              <button
                type="button"
                className={`${styles.secondaryButton} ${!selectedFolderId ? styles.folderScopeActive : ""}`}
                onClick={() => startCreateTestCaseInFolder(selectedFolderId, selectedFolderName)}
                disabled={authBlocked}
              >
                + Test Case
              </button>
            </div>
            <p className={styles.metaLine}>Current folder: {selectedFolderName}</p>

            {savedCases.length === 0 && folders.length === 0 ? (
              <p className={styles.emptyState}>No folders or test cases yet.</p>
            ) : (
              <div className={styles.savedList}>{renderLibraryNodes(null, 0)}</div>
            )}

          </section>
        </div>

        <section className={`${styles.panel} ${styles.resultPanel}`}>
          <h2>Result</h2>
          {!currentRun && !currentSuiteRun ? (
            <p className={styles.emptyState}>No result yet. Submit a prompt to run automation.</p>
          ) : null}
          {currentSuiteRun ? (
            <div className={styles.planPreview}>
              <div className={styles.runHeader}>
                <div>
                  <p className={styles.runName}>{currentSuiteRun.suite_name}</p>
                  <p className={styles.metaLine}>Suite ID: {currentSuiteRun.suite_run_id}</p>
                </div>
                <p className={`${styles.statusPill} ${statusClass(currentSuiteRun.status)}`}>
                  {currentSuiteRun.status}
                </p>
              </div>
              <p className={styles.metaLine}>
                Tests: {currentSuiteRun.tests.length}
                {suiteIsActive ? " · Running in parallel..." : ""}
              </p>
              {suiteReportUrl ? (
                <a className={styles.secondaryButton} href={suiteReportUrl} target="_blank" rel="noopener noreferrer">
                  Open Suite Report
                </a>
              ) : null}
              <div className={styles.timeline}>
                {currentSuiteRun.tests.map((suiteTest) => (
                  <article key={`suite-test-${suiteTest.test_case_id}`} className={styles.timelineItem}>
                    <div className={styles.timelineTop}>
                      <p>{suiteTest.name}</p>
                      <p className={`${styles.statusPill} ${statusClass(suiteTest.status)}`}>{suiteTest.status}</p>
                    </div>
                    {suiteTest.run_id ? (
                      <a
                        className={styles.secondaryButton}
                        href={`${API_BASE_URL}/api/runs/${suiteTest.run_id}/artifacts/report.html`}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        Open Test Report
                      </a>
                    ) : null}
                  </article>
                ))}
              </div>
            </div>
          ) : null}
          {currentRun ? (
            <>
              <div className={styles.runHeader}>
                <div>
                  <p className={styles.runName}>{currentRun.run_name}</p>
                  <p className={styles.metaLine}>Run ID: {currentRun.run_id}</p>
                </div>
                <div className={styles.runHeaderActions}>
                  {reportUrl ? (
                    <a
                      className={styles.secondaryButton}
                      href={reportUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      View Report
                    </a>
                  ) : null}
                  <p className={`${styles.statusPill} ${statusClass(currentRun.status)}`}>
                    {currentRun.status}
                  </p>
                </div>
              </div>

              {currentRun.summary ? <p className={styles.summary}>{currentRun.summary}</p> : null}

              <div className={styles.timeline}>
                {currentRun.steps.map((step) => (
                  <article key={step.step_id} className={styles.timelineItem}>
                    <div className={styles.timelineTop}>
                      <p>
                        #{step.index + 1} {formatStepType(step.type)}
                      </p>
                      <p className={`${styles.statusPill} ${statusClass(step.status)}`}>
                        {step.status}
                      </p>
                    </div>
                    {step.message ? <p className={styles.stepMessage}>{step.message}</p> : null}
                    {step.error ? (
                      <p className={styles.stepError}>{step.error}</p>
                    ) : step.status === "failed" ? (
                      <p className={styles.stepError}>
                        Step failed with no details returned. Re-run once and check backend logs with Run ID:
                        {" "}
                        {currentRun.run_id}
                      </p>
                    ) : null}
                    {(step.status === "failed" || (step.status === "waiting_for_input" && step.user_input_kind === "selector")) && editableSelectorField(step) ? (
                      <div className={styles.selectorFixBox}>
                        <p className={styles.selectorFixTitle}>Selector Recovery</p>
                        <p className={styles.selectorFixHint}>
                          {step.user_input_prompt
                            ? step.user_input_prompt
                            : "Paste a corrected selector from your inspector tool and continue this step directly."}
                          {currentRunSourceTestCaseId ? " The saved test case will also be updated." : ""}
                        </p>
                        <div className={styles.selectorFixRow}>
                          <input
                            className={styles.selectorFixInput}
                            value={selectorFixInputs[step.step_id] ?? defaultSelectorForStep(step)}
                            onChange={(event) =>
                              setSelectorFixInputs((previous) => ({
                                ...previous,
                                [step.step_id]: event.target.value,
                              }))
                            }
                            placeholder="Paste corrected selector"
                          />
                          <button
                            type="button"
                            className={styles.secondaryButton}
                            onClick={() => void applySelectorFixAndRerun(step)}
                            disabled={Boolean(selectorFixBusyByStepId[step.step_id])}
                          >
                            {selectorFixBusyByStepId[step.step_id] ? "Applying..." : "Apply & Continue"}
                          </button>
                        </div>
                      </div>
                    ) : null}
                  </article>
                ))}
              </div>
            </>
          ) : null}
        </section>
      </main>
    </div>
  );
}

