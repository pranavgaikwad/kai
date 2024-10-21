import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional, cast
from unittest.mock import MagicMock
from urllib.parse import urlparse

import requests
from pydantic import BaseModel

from kai.jsonrpc.core import JsonRpcApplication, JsonRpcServer
from kai.jsonrpc.logs import JsonRpcLoggingHandler
from kai.jsonrpc.models import JsonRpcError, JsonRpcErrorCode, JsonRpcId
from kai.jsonrpc.util import DEFAULT_FORMATTER, TRACE, CamelCaseBaseModel
from kai.kai_trace import KaiTrace
from kai.models.file_solution import (
    FileSolutionContent,
    guess_language,
    parse_file_solution_content,
)
from kai.models.kai_config import KaiConfigModels
from kai.models.report_types import ExtendedIncident, Incident, RuleSet, Violation
from kai.repo_level_awareness.agent.dependency_agent.dependency_agent import (
    MavenDependencyAgent,
)
from kai.repo_level_awareness.agent.reflection_agent import ReflectionAgent
from kai.repo_level_awareness.api import RpcClientConfig, Task, TaskResult
from kai.repo_level_awareness.codeplan import TaskManager
from kai.repo_level_awareness.task_runner.analyzer_lsp.api import AnalyzerRuleViolation
from kai.repo_level_awareness.task_runner.analyzer_lsp.task_runner import (
    AnalyzerTaskRunner,
)
from kai.repo_level_awareness.task_runner.analyzer_lsp.validator import AnalyzerLSPStep
from kai.repo_level_awareness.task_runner.compiler.compiler_task_runner import (
    MavenCompilerTaskRunner,
)
from kai.repo_level_awareness.task_runner.compiler.maven_validator import (
    MavenCompileStep,
)
from kai.repo_level_awareness.task_runner.dependency.task_runner import (
    DependencyTaskRunner,
)
from kai.repo_level_awareness.vfs.git_vfs import RepoContextManager, RepoContextSnapshot
from kai.routes.get_solutions import PostGetSolutionsParams, ResponseGetSolutions
from kai.rpc_server.util import get_prompt, playback_if_demo_mode
from kai.service.llm_interfacing.model_provider import ModelProvider
from kai.service.solution_handling.consumption import (
    SolutionConsumerKind,
    solution_consumer_factory,
)


class KaiRpcApplicationConfig(CamelCaseBaseModel):
    process_id: Optional[int]

    root_path: Path
    model_provider: KaiConfigModels
    solution_consumer: SolutionConsumerKind = SolutionConsumerKind.DIFF_ONLY
    kai_backend_url: str

    log_level: str = "INFO"
    stderr_log_level: str = "TRACE"
    file_log_level: Optional[str] = None
    log_dir_path: Optional[Path] = None
    demo_mode: bool = False


class KaiRpcApplication(JsonRpcApplication):
    def __init__(self) -> None:
        super().__init__()

        self.initialized = False
        self.config: Optional[KaiRpcApplicationConfig] = None
        self.log = logging.getLogger("kai_rpc_application")


app = KaiRpcApplication()

ERROR_NOT_INITIALIZED = JsonRpcError(
    code=JsonRpcErrorCode.ServerErrorStart,
    message="Server not initialized",
)


@app.add_request(method="echo")
def echo(
    app: KaiRpcApplication, server: JsonRpcServer, id: JsonRpcId, params: dict[str, Any]
) -> None:
    server.send_response(id=id, result=params)


@app.add_request(method="shutdown")
def shutdown(
    app: KaiRpcApplication, server: JsonRpcServer, id: JsonRpcId, params: dict[str, Any]
) -> None:
    server.shutdown_flag = True

    server.send_response(id=id, result={})


@app.add_request(method="exit")
def exit(
    app: KaiRpcApplication, server: JsonRpcServer, id: JsonRpcId, params: dict[str, Any]
) -> None:
    server.shutdown_flag = True

    server.send_response(id=id, result={})


# NOTE(shawn-hurley): would it ever make sense to have the server
# "re-initialized" or would you just shutdown and restart the process?
@app.add_request(method="initialize")
def initialize(
    app: KaiRpcApplication,
    server: JsonRpcServer,
    id: JsonRpcId,
    params: KaiRpcApplicationConfig,
) -> None:
    if app.initialized:
        server.send_response(
            id=id,
            error=JsonRpcError(
                code=JsonRpcErrorCode.ServerErrorStart,
                message="Server already initialized",
            ),
        )
        return

    try:
        app.config = params

        app.config.root_path = app.config.root_path.resolve()
        if app.config.log_dir_path:
            app.config.log_dir_path = app.config.log_dir_path.resolve()

        app.log.setLevel(TRACE)
        app.log.handlers.clear()
        app.log.filters.clear()

        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setLevel(TRACE)
        stderr_handler.setFormatter(DEFAULT_FORMATTER)
        app.log.addHandler(stderr_handler)

        notify_handler = JsonRpcLoggingHandler(server)
        notify_handler.setLevel(app.config.log_level)
        notify_handler.setFormatter(DEFAULT_FORMATTER)
        app.log.addHandler(notify_handler)

        if app.config.file_log_level and app.config.log_dir_path:
            log_file = app.config.log_dir_path / "kai_rpc.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)

            file_handler = logging.FileHandler(log_file)
            file_handler.setLevel(app.config.file_log_level)
            file_handler.setFormatter(DEFAULT_FORMATTER)
            app.log.addHandler(file_handler)

        app.log.info(f"Initialized with config: {app.config}")

    except Exception:
        server.send_response(
            id=id,
            error=JsonRpcError(
                code=JsonRpcErrorCode.InternalError,
                message=str(traceback.format_exc()),
            ),
        )
        return

    app.initialized = True

    server.send_response(id=id, result=app.config.model_dump())


# NOTE(shawn-hurley): I would just as soon make this another initialize call
# rather than a separate endpoint. but open to others feedback.
@app.add_request(method="setConfig")
def set_config(
    app: KaiRpcApplication, server: JsonRpcServer, id: JsonRpcId, params: dict[str, Any]
) -> None:
    if not app.initialized:
        server.send_response(id=id, error=ERROR_NOT_INITIALIZED)
        return
    app.config = cast(KaiRpcApplicationConfig, app.config)

    # Basically reset everything
    app.initialized = False
    try:
        initialize.func(app, server, id, KaiRpcApplicationConfig.model_validate(params))
    except Exception as e:
        server.send_response(
            id=id,
            error=JsonRpcError(
                code=JsonRpcErrorCode.InternalError,
                message=str(e),
            ),
        )


class GetRagSolutionParams(BaseModel):
    file_path: Path
    incidents: list[ExtendedIncident]
    trace_enabled: bool = False
    include_solved_incidents: bool = False


@app.add_request(method="getRAGSolution")
def get_rag_solution(
    app: KaiRpcApplication,
    server: JsonRpcServer,
    id: JsonRpcId,
    params: GetRagSolutionParams,
) -> None:
    if not app.initialized:
        server.send_response(id=id, error=ERROR_NOT_INITIALIZED)
        return

    app.config = cast(KaiRpcApplicationConfig, app.config)

    try:
        model_provider = ModelProvider(app.config.model_provider)
    except Exception as e:
        server.send_response(
            id=id,
            error=JsonRpcError(
                code=JsonRpcErrorCode.InternalError,
                message=str(e),
            ),
        )
        return

    try:
        app.log.info(f"get_rag_solution called {params.model_dump()}")

        file_contents = ""
        with open(app.config.root_path / params.file_path, "r") as f:
            file_contents = f.read()

        trace = KaiTrace(
            trace_enabled=params.trace_enabled,
            log_dir=str(app.config.log_dir_path),
            model_id=model_provider.model_id,
            batch_mode="single_group",
            file_name=params.file_path.name,
            application_name=app.config.root_path.name,
        )

        source_language = guess_language(file_contents, params.file_path.name)

        app.log.info(
            f"Processing {len(params.incidents)} incident(s) for {params.file_path}"
        )

        pb_incidents = [incident.model_dump() for incident in params.incidents]

        if params.include_solved_incidents:
            for pb_incident in pb_incidents:
                solution_consumer = solution_consumer_factory(
                    app.config.solution_consumer
                )
                try:
                    headers = {"Content-type": "application/json"}
                    response = requests.post(
                        f"{app.config.kai_backend_url}/get_solutions",
                        headers=headers,
                        data=PostGetSolutionsParams(
                            ruleset_name=pb_incident["ruleset_name"],
                            violation_name=pb_incident["violation_name"],
                            incident_variables=pb_incident["incident_variables"],
                            incident_snip=pb_incident["code_snip"],
                            limit=1,
                        ).model_dump_json(),
                    )
                    if response.status_code == 200:
                        parsed_res = ResponseGetSolutions.model_validate_json(
                            response.json()
                        )
                        if len(parsed_res.solutions) != 0:
                            solution_str = solution_consumer(parsed_res.solutions[0])

                        if len(solution_str) != 0:
                            pb_incident["solution_str"] = solution_str
                    else:
                        app.log.debug(
                            f"Failed getting solved incidents, response code {response.status_code}"
                        )
                except Exception as e:
                    app.log.error(f"Failed getting solved incidents - {e}")
        pb_vars = {
            "src_file_name": str(params.file_path),
            "src_file_language": source_language,
            "src_file_contents": file_contents,
            "incidents": pb_incidents,
            "model_provider": model_provider,
        }

        # Render the prompt
        count = 0
        prompt = get_prompt(model_provider.template, pb_vars)
        trace.prompt(count, prompt, pb_vars)
        result = FileSolutionContent(
            reasoning="",
            updated_file="",
            additional_info="",
            used_prompts=[],
            llm_results=[],
        )
        app.log.debug(f"Sending prompt: {prompt}")
        llm_result = None
        for retry_attempt_count in range(model_provider.llm_retries):
            try:
                with playback_if_demo_mode(
                    app.config.demo_mode,
                    model_provider.model_id,
                    app.config.root_path.name,
                    f'{str(params.file_path).replace(os.path.sep, "-")}',
                ):
                    app.log.info("calling model")
                    llm_result = model_provider.llm.invoke(prompt)
                    trace.llm_result(count, retry_attempt_count, llm_result)

                    content = parse_file_solution_content(
                        source_language, str(llm_result.content)
                    )

                    if not content.updated_file:
                        raise Exception(
                            f"Error in LLM Response: The LLM did not provide an updated file for {str(params.file_path)}"
                        )

                    result.updated_file = content.updated_file
                    result.llm_results.append(llm_result.content)
                    result.used_prompts.append(prompt)
                    result.reasoning = content.reasoning
                    result.additional_info = content.additional_info

                    server.send_response(
                        id=id,
                        result=result.model_dump(),
                    )
                    return
            except Exception as e:
                app.log.info(
                    f"Request to model failed for attempt {retry_attempt_count}/{model_provider.llm_retries} for {params.file_path} with exception {e}, retrying in {model_provider.llm_retry_delay}s"
                )
                app.log.debug(traceback.format_exc())
                trace.exception(count, retry_attempt_count, e, traceback.format_exc())
                time.sleep(model_provider.llm_retry_delay)

        app.log.error(f"Failed to generate updated file for {params.file_path}")
        server.send_response(
            id=id,
            error=JsonRpcError(
                code=JsonRpcErrorCode.InternalError,
                message=f"Failed to generate updated file for {params.file_path}",
            ),
        )
    except Exception:
        server.send_response(
            id=id,
            error=JsonRpcError(
                code=JsonRpcErrorCode.InternalError,
                message=str(traceback.format_exc()),
            ),
        )


class GetCodeplanAgentSolutionParams(BaseModel):
    file_path: Path
    incidents: list[ExtendedIncident]
    analyzer_lsp_lsp_path: Path
    analyzer_lsp_rpc_path: Path
    analyzer_lsp_rules_path: Path
    analyzer_lsp_java_bundle_path: Path


class GitVFSUpdateParams(BaseModel):
    work_tree: str  # project root
    git_dir: str
    git_sha: str
    diff: str
    msg: str
    spawning_result: Optional[str]

    children: list["GitVFSUpdateParams"]

    @classmethod
    def from_snapshot(cls, snapshot: RepoContextSnapshot) -> "GitVFSUpdateParams":
        if snapshot.parent:
            diff_result = snapshot.diff(snapshot.parent)
            diff = diff_result[1] + diff_result[2]
        else:
            diff = ""

        try:
            spawning_result = repr(snapshot.spawning_result)
        except Exception:
            spawning_result = ""

        return cls(
            work_tree=str(snapshot.work_tree),
            git_dir=str(snapshot.git_dir),
            git_sha=snapshot.git_sha,
            diff=diff,
            msg=snapshot.msg,
            children=[cls.from_snapshot(c) for c in snapshot.children],
            spawning_result=spawning_result,
        )

    # spawning_result: Optional[SpawningResult] = None


class TestRCMParams(BaseModel):
    rcm_root: Path
    file_path: Path
    new_content: str


@app.add_request(method="testRCM")
def test_rcm(
    app: KaiRpcApplication,
    server: JsonRpcServer,
    id: JsonRpcId,
    params: TestRCMParams,
) -> None:
    rcm = RepoContextManager(
        project_root=params.rcm_root,
        reflection_agent=ReflectionAgent(
            llm=MagicMock(),
        ),
    )

    with open(params.file_path, "w") as f:
        f.write(params.new_content)

    rcm.commit("testRCM")

    diff = rcm.snapshot.diff(rcm.first_snapshot)

    rcm.reset_to_first()

    server.send_response(
        id=id,
        result={
            "diff": diff[1] + diff[2],
        },
    )


@app.add_request(method="getCodeplanAgentSolution")
def get_codeplan_agent_solution(
    app: KaiRpcApplication,
    server: JsonRpcServer,
    id: JsonRpcId,
    params: GetCodeplanAgentSolutionParams,
) -> None:
    # create a set of AnalyzerRuleViolations
    # seed the task manager with these violations
    # get the task with priority 0 and do the whole thingy

    app.log.info(f"get_codeplan_agent_solution: {params}")
    if not app.initialized:
        server.send_response(id=id, error=ERROR_NOT_INITIALIZED)
        return

    app.config = cast(KaiRpcApplicationConfig, app.config)

    try:
        model_provider = ModelProvider(app.config.model_provider)
        params.analyzer_lsp_rpc_path = params.analyzer_lsp_rpc_path.resolve()
        params.analyzer_lsp_java_bundle_path = (
            params.analyzer_lsp_java_bundle_path.resolve()
        )
        params.analyzer_lsp_lsp_path = params.analyzer_lsp_lsp_path.resolve()
        params.analyzer_lsp_rules_path = params.analyzer_lsp_rules_path.resolve()
    except Exception as e:
        server.send_response(
            id=id,
            error=JsonRpcError(
                code=JsonRpcErrorCode.InternalError,
                message=str(e),
            ),
        )
        return

    # Data for AnalyzerRuleViolation should probably take an ExtendedIncident
    seed_tasks: list[Task] = []

    for incident in params.incidents:
        seed_tasks.append(
            AnalyzerRuleViolation(
                file=urlparse(incident.uri).path,
                line=incident.line_number,
                column=-1,  # Not contained within report?
                message=incident.message,
                priority=0,
                incident=Incident(**incident.model_dump()),
                violation=Violation(
                    description=incident.violation_description or "",
                    category=incident.violation_category,
                    labels=incident.violation_labels,
                ),
                ruleset=RuleSet(
                    name=incident.ruleset_name,
                    description=incident.ruleset_description or "",
                ),
            )
        )

    rcm = RepoContextManager(
        project_root=app.config.root_path,
        reflection_agent=ReflectionAgent(
            llm=model_provider.llm, iterations=1, retries=3
        ),
    )

    server.send_notification(
        "gitVFSUpdate",
        GitVFSUpdateParams.from_snapshot(rcm.first_snapshot).model_dump(),
    )

    task_manager_config = RpcClientConfig(
        repo_directory=app.config.root_path,
        analyzer_lsp_server_binary=params.analyzer_lsp_rpc_path,
        rules_directory=params.analyzer_lsp_rules_path,
        analyzer_lsp_path=params.analyzer_lsp_lsp_path,
        analyzer_java_bundle_path=params.analyzer_lsp_java_bundle_path,
        label_selector="konveyor.io/target=quarkus || konveyor.io/target=jakarta-ee",
        incident_selector=None,
        included_paths=None,
    )

    task_manager = TaskManager(
        config=task_manager_config,
        rcm=rcm,
        seed_tasks=seed_tasks,
        validators=[
            MavenCompileStep(task_manager_config),
            AnalyzerLSPStep(task_manager_config),
        ],
        agents=[
            AnalyzerTaskRunner(model_provider.llm),
            MavenCompilerTaskRunner(model_provider.llm),
            DependencyTaskRunner(
                MavenDependencyAgent(model_provider.llm, app.config.root_path)
            ),
        ],
    )

    flag = False
    result: TaskResult
    for task in task_manager.get_next_task(0):
        if flag:
            break

        app.log.debug(f"Executing task {task.__class__.__name__}: {task}")

        result = task_manager.execute_task(task)

        app.log.debug(f"Task {task.__class__.__name__} result: {result}")

        task_manager.supply_result(result)

        app.log.debug(f"Executed task {task.__class__.__name__}")
        rcm.commit(f"Executed task {task.__class__.__name__}")

        server.send_notification(
            "gitVFSUpdate",
            GitVFSUpdateParams.from_snapshot(rcm.first_snapshot).model_dump(),
        )

        flag = True

    # FIXME: This is a hack to stop the task_manager as it's hanging trying to stop everything
    threading.Thread(target=task_manager.stop).start()

    diff = rcm.snapshot.diff(rcm.first_snapshot)

    rcm.reset_to_first()

    server.send_response(
        id=id,
        result={
            "encountered_errors": [str(e) for e in result.encountered_errors],
            "modified_files": [str(f) for f in result.modified_files],
            "diff": diff[1] + diff[2],
        },
    )
