from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from html import escape

from app.config import Settings
from app.mcp.filesystem_client import FileSystemClient
from app.runtime.executor import AgentExecutor
from app.runtime.store import RunStore
from app.runtime.suite_store import SuiteStore
from app.runtime.test_case_store import TestCaseStore
from app.runtime.viewer_session import ViewerSessionManager
from app.schemas import RunCreateRequest, SuiteRunState, SuiteRunStatus

LOGGER = logging.getLogger("tekno.phantom.suite_executor")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SuiteExecutor:
    def __init__(
        self,
        settings: Settings,
        run_store: RunStore,
        suite_store: SuiteStore,
        test_case_store: TestCaseStore,
        run_executor: AgentExecutor,
        file_client: FileSystemClient,
        viewer_sessions: ViewerSessionManager | None = None,
    ) -> None:
        self._settings = settings
        self._run_store = run_store
        self._suite_store = suite_store
        self._test_case_store = test_case_store
        self._run_executor = run_executor
        self._files = file_client
        self._viewer_sessions = viewer_sessions

    async def execute(self, suite_run_id: str) -> None:
        suite_run = self._suite_store.get(suite_run_id)
        if not suite_run:
            return

        suite_run.status = SuiteRunStatus.running
        suite_run.started_at = utc_now()
        LOGGER.info(
            "Suite run %s: starting '%s' with %d test(s) (max_parallel=%d)",
            suite_run_id,
            suite_run.suite_name,
            len(suite_run.tests),
            suite_run.max_parallel,
        )
        self._suite_store.persist(suite_run)

        max_parallel = max(1, min(suite_run.max_parallel, 10))
        semaphore = asyncio.Semaphore(max_parallel)

        async def run_one(test_case_id: str) -> None:
            async with semaphore:
                await self._execute_single_test(suite_run_id, test_case_id)

        try:
            await asyncio.gather(*(run_one(item.test_case_id) for item in suite_run.tests))
            refreshed = self._suite_store.get(suite_run_id)
            if not refreshed:
                return
            if refreshed.status != SuiteRunStatus.cancelled:
                any_failed = any(test.status == SuiteRunStatus.failed for test in refreshed.tests)
                refreshed.status = SuiteRunStatus.failed if any_failed else SuiteRunStatus.completed
            refreshed.finished_at = utc_now()
            refreshed.summary = self._build_summary(refreshed)
            LOGGER.info("Suite run %s: finished with status=%s", suite_run_id, refreshed.status.value)
            await self._write_suite_report(refreshed)
            self._suite_store.persist(refreshed)
        except Exception:
            LOGGER.exception("Suite run failed unexpectedly: %s", suite_run_id)
            refreshed = self._suite_store.get(suite_run_id)
            if refreshed:
                refreshed.status = SuiteRunStatus.failed
                refreshed.finished_at = utc_now()
                refreshed.summary = "Suite run failed unexpectedly."
                self._suite_store.persist(refreshed)
        finally:
            self._suite_store.clear_cancel(suite_run_id)

    async def _execute_single_test(self, suite_run_id: str, test_case_id: str) -> None:
        suite_run = self._suite_store.get(suite_run_id)
        if not suite_run:
            return
        if self._suite_store.is_cancelled(suite_run_id):
            suite_run.status = SuiteRunStatus.cancelled
            for test in suite_run.tests:
                if test.status in (SuiteRunStatus.pending, SuiteRunStatus.running):
                    test.status = SuiteRunStatus.cancelled
            self._suite_store.persist(suite_run)
            return

        target = next((item for item in suite_run.tests if item.test_case_id == test_case_id), None)
        if not target:
            return

        target.status = SuiteRunStatus.running
        target.started_at = utc_now()
        LOGGER.info("Suite run %s: starting test_case_id=%s name=%r", suite_run_id, test_case_id, target.name)
        self._suite_store.persist(suite_run)

        test_case = self._test_case_store.get(test_case_id)
        if not test_case:
            target.status = SuiteRunStatus.failed
            target.finished_at = utc_now()
            target.error = "Test case not found"
            self._suite_store.persist(suite_run)
            return

        request = RunCreateRequest.model_validate(
            {
                "run_name": test_case.name,
                "start_url": test_case.start_url,
                "steps": [step.model_dump(exclude_none=True) for step in test_case.steps],
                "test_data": test_case.test_data,
                "selector_profile": test_case.selector_profile,
            }
        )
        run = self._run_store.create(request, user_id=test_case.user_id)
        target.run_id = run.run_id
        if self._viewer_sessions is not None:
            info = self._viewer_sessions.prepare_run(run.run_id)
            if info is not None:
                run.viewer_token = info.token
                run.viewer_url = info.viewer_url
                run.viewer_status = info.status
                self._run_store.persist(run)
                target.viewer_url = info.viewer_url
        self._suite_store.persist(suite_run)

        try:
            await self._run_executor.execute(run.run_id)
            completed_run = self._run_store.get(run.run_id)
            if not completed_run:
                raise ValueError("Run state missing after execution")
            target.status = (
                SuiteRunStatus.completed if completed_run.status.value == "completed" else SuiteRunStatus.failed
            )
            target.summary = completed_run.summary
            target.report_artifact = completed_run.report_artifact
            target.error = completed_run.summary if target.status == SuiteRunStatus.failed else None
            LOGGER.info(
                "Suite run %s: test_case_id=%s finished status=%s",
                suite_run_id, test_case_id, target.status.value,
            )
        except Exception as exc:
            target.status = SuiteRunStatus.failed
            target.error = str(exc)
            LOGGER.exception("Suite run %s: test_case_id=%s raised an exception", suite_run_id, test_case_id)
        finally:
            target.finished_at = utc_now()
            self._suite_store.persist(suite_run)

    async def _write_suite_report(self, suite_run: SuiteRunState) -> None:
        try:
            html = self._build_html_report(suite_run)
            report_path = await self._files.write_text_artifact(
                suite_run.suite_run_id,
                "suite-report.html",
                html,
            )
            suite_run.report_artifact = report_path
        except Exception:
            LOGGER.exception("Failed writing suite report for %s", suite_run.suite_run_id)

    @staticmethod
    def _build_summary(suite_run: SuiteRunState) -> str:
        passed = sum(1 for item in suite_run.tests if item.status == SuiteRunStatus.completed)
        failed = sum(1 for item in suite_run.tests if item.status == SuiteRunStatus.failed)
        cancelled = sum(1 for item in suite_run.tests if item.status == SuiteRunStatus.cancelled)
        total = len(suite_run.tests)
        return (
            f"Suite '{suite_run.suite_name}' finished with status {suite_run.status.value}. "
            f"Total={total}, Passed={passed}, Failed={failed}, Cancelled={cancelled}."
        )

    @staticmethod
    def _build_html_report(suite_run: SuiteRunState) -> str:
        rows: list[str] = []
        embedded_reports: list[str] = []
        for index, item in enumerate(suite_run.tests):
            artifact = "-"
            if item.run_id:
                report_url = f"/api/runs/{escape(item.run_id)}/artifacts/report.html"
                artifact = f'<a href="{report_url}" target="_blank" rel="noopener">Test Report</a>'
                embedded_reports.append(
                    "<section class='embedded-report'>"
                    "<details open>"
                    "<summary>"
                    f"{escape(item.name)} - {escape(item.status.value)} "
                    f"(Run ID: {escape(item.run_id)})"
                    "</summary>"
                    "<div class='embedded-actions'>"
                    f"<a href='{report_url}' target='_blank' rel='noopener'>Open This Test Report</a>"
                    "</div>"
                    f"<iframe src='{report_url}' title='Report for {escape(item.name)}'></iframe>"
                    "</details>"
                    "</section>"
                )
            status_class = f"status-{item.status.value}"
            rows.append(
                "<tr>"
                f"<td>{index + 1}</td>"
                f"<td>{escape(item.name)}</td>"
                f"<td class='{status_class}'>{escape(item.status.value)}</td>"
                f"<td>{escape(item.run_id or '-')}</td>"
                f"<td>{escape(item.summary or item.error or '-')}</td>"
                f"<td>{artifact}</td>"
                "</tr>"
            )
        table_rows = "\n".join(rows) if rows else "<tr><td colspan='6'>No tests in suite.</td></tr>"
        embedded_html = (
            "\n".join(embedded_reports)
            if embedded_reports
            else "<p class='meta'>No individual test reports were generated for this suite.</p>"
        )
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Suite Report - {escape(suite_run.suite_name)}</title>
  <style>
    :root {{
      --bg: #000000;
      --card: #111111;
      --border: rgba(255,255,255,0.10);
      --border-strong: rgba(255,255,255,0.16);
      --text: #ffffff;
      --muted: #999999;
      --accent: #FFB300;
      --pass: #22c55e;
      --fail: #ef4444;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 24px;
      background: var(--bg);
      color: var(--text);
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
    }}
    .card {{
      max-width: 1100px;
      margin: 0 auto;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 24px;
    }}
    h1 {{
      margin: 0 0 6px 0;
      font-size: 24px;
      color: var(--accent);
    }}
    h2 {{ margin: 24px 0 10px 0; font-size: 18px; color: var(--accent); }}
    .meta {{ color: var(--muted); font-size: 13px; margin-bottom: 4px; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
    th, td {{
      border: 1px solid var(--border);
      padding: 10px 12px;
      text-align: left;
      font-size: 13px;
      vertical-align: top;
    }}
    th {{ background: rgba(255,179,0,0.08); color: var(--accent); font-weight: 600; }}
    td {{ color: var(--text); }}
    tr:hover td {{ background: rgba(255,255,255,0.03); }}
    td.status-completed {{ color: var(--pass); font-weight: 600; }}
    td.status-failed {{ color: var(--fail); font-weight: 600; }}
    td.status-cancelled {{ color: var(--muted); font-weight: 600; }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .embedded-report {{
      margin-top: 12px;
      border: 1px solid var(--border);
      border-radius: 10px;
      background: #0a0a0a;
      overflow: hidden;
    }}
    .embedded-report summary {{
      cursor: pointer;
      padding: 12px 14px;
      font-weight: 600;
      font-size: 14px;
      background: rgba(255,179,0,0.06);
      color: var(--text);
      border-bottom: 1px solid var(--border);
    }}
    .embedded-actions {{
      padding: 10px 14px 0;
      font-size: 13px;
    }}
    .embedded-report iframe {{
      display: block;
      width: 100%;
      min-height: 700px;
      border: none;
      background: #000;
      margin-top: 8px;
    }}
  </style>
</head>
<body>
  <main class="card">
    <h1>Suite Execution Report</h1>
    <div class="meta">Suite Name: {escape(suite_run.suite_name)}</div>
    <div class="meta">Suite Run ID: {escape(suite_run.suite_run_id)}</div>
    <div class="meta">Status: {escape(suite_run.status.value)}</div>
    <div class="meta">Summary: {escape(suite_run.summary or '-')}</div>
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>Test Case</th>
          <th>Status</th>
          <th>Run ID</th>
          <th>Summary/Error</th>
          <th>Artifact</th>
        </tr>
      </thead>
      <tbody>
        {table_rows}
      </tbody>
    </table>
    <h2>Individual Test Reports</h2>
    {embedded_html}
  </main>
</body>
</html>
"""
