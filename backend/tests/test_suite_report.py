from app.runtime.suite_executor import SuiteExecutor
from app.schemas import SuiteRunState, SuiteRunStatus, SuiteTestState


def test_suite_report_embeds_individual_test_reports() -> None:
    suite_run = SuiteRunState(
        suite_name="selected-tests-suite",
        status=SuiteRunStatus.completed,
        tests=[
            SuiteTestState(
                test_case_id="tc-1",
                name="t1",
                status=SuiteRunStatus.completed,
                run_id="run-1",
                summary="ok",
            ),
            SuiteTestState(
                test_case_id="tc-2",
                name="amazon",
                status=SuiteRunStatus.completed,
                run_id="run-2",
                summary="ok",
            ),
        ],
        summary="suite ok",
    )

    html = SuiteExecutor._build_html_report(suite_run)

    assert "Individual Test Reports" in html
    assert "/api/runs/run-1/artifacts/report.html" in html
    assert "/api/runs/run-2/artifacts/report.html" in html
    assert "<iframe" in html
    assert "Open This Test Report" in html
