"""由宿主启动的 pytest 驱动；配置与结果文件位于候选项目之外。"""

import json
import sys
from pathlib import Path

import pytest

# 目标环境只需 pytest 和产品依赖；框架的可信驱动从自身源码加载。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from miniharness.verification.oracle import configure


class Results:
    def __init__(self):
        self.collected = []
        self.descriptions = {}
        self.reports = []
        self.collection_errors = []
        self.collection_failures = []
        self.deselected = []
        self.failures = []

    def pytest_deselected(self, items):
        self.deselected.extend(item.nodeid for item in items)

    def pytest_collection_finish(self, session):
        self.collected = [item.nodeid for item in session.items]
        for item in session.items:
            if isinstance(item, pytest.Function):
                doc = getattr(item.function, "__doc__", None)
                if isinstance(doc, str) and doc.strip():
                    self.descriptions[item.nodeid] = doc.strip().splitlines()[0][:500]

    def pytest_collectreport(self, report):
        if report.failed or report.skipped:
            self.collection_errors.append(str(report.longrepr))
            self.collection_failures.append({"id": report.nodeid})

    def pytest_runtest_logreport(self, report):
        xfail = hasattr(report, "wasxfail")
        if report.failed or report.skipped or xfail:
            crash = getattr(report.longrepr, "reprcrash", None)
            if xfail:
                message = f"测试标记为预期失败（xfail）：{report.wasxfail}"
            elif report.skipped:
                reason = (
                    report.longrepr[2] if isinstance(report.longrepr, tuple) else report.longrepr
                )
                message = f"测试被跳过：{reason}"
            else:
                message = getattr(crash, "message", report.longrepr or "测试执行失败，详见完整日志")
            failure = {"id": report.nodeid, "message": str(message)[:1800]}
            if crash and getattr(crash, "path", None):
                location = Path(crash.path).resolve()
                if location.is_file() and location.is_relative_to(Path.cwd()):
                    failure["source_path"] = location.relative_to(Path.cwd()).as_posix()
            self.failures.append(failure)
        self.reports.append(
            {
                "id": report.nodeid,
                "when": report.when,
                "outcome": report.outcome,
                "xfail": xfail,
            }
        )


def main():
    target, destination, configuration, *settings_path = sys.argv[1:]
    if settings_path:
        settings = json.loads(Path(settings_path[0]).read_text(encoding="utf-8"))
        configure(Path(settings["project_root"]), settings["timeout"])
    # pytest 本身已从宿主安装加载，再开放被测项目的导入路径。
    sys.path.insert(0, str(Path.cwd()))
    results = Results()
    # Windows 上反复重定向原生文件句柄会干扰 Tcl/Tk 初始化。外层进程仍捕获完整 stdout/stderr。
    capture = ["--capture=sys"] if sys.platform == "win32" else []
    code = pytest.main(
        [
            "-q",
            "-ra",
            *capture,
            "-c",
            configuration,
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            target,
        ],
        plugins=[results],
    )
    Path(destination).write_text(
        json.dumps(
            {
                "exit_code": int(code),
                "collected": results.collected,
                "descriptions": results.descriptions,
                "reports": results.reports,
                "collection_errors": results.collection_errors,
                "collection_failures": results.collection_failures,
                "deselected": results.deselected,
                "failures": results.failures,
            }
        ),
        encoding="utf-8",
    )
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
