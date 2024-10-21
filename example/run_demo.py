#!/usr/bin/env python
import contextlib
import json
import logging
import os
import subprocess
import sys
import time
from io import BufferedReader, BufferedWriter
from pathlib import Path
from typing import Generator, cast

# Ensure that we have 'kai' in our import path
sys.path.append("../../kai")
from kai.jsonrpc.core import JsonRpcError, JsonRpcId, JsonRpcResponse, JsonRpcServer
from kai.jsonrpc.streams import BareJsonStream
from kai.kai_logging import formatter
from kai.models.file_solution import FileSolutionContent
from kai.models.kai_config import KaiConfig
from kai.models.report import Report
from kai.models.report_types import ExtendedIncident
from kai.rpc_server.server import (
    GetRagSolutionParams,
    KaiRpcApplication,
    KaiRpcApplicationConfig,
)

KAI_LOG = logging.getLogger("run_demo")

SERVER_URL = "http://0.0.0.0:8080"
APP_NAME = "coolstore"
SAMPLE_APP_DIR = "./coolstore"


# TODOs
# 1) Add ConfigFile to tweak the server URL and rulesets/violations
# 2) Limit to specific rulesets/violations we are interested in


@contextlib.contextmanager
def initialize_rpc_server() -> Generator[JsonRpcServer, None, None]:
    kai_config = KaiConfig.model_validate_filepath("config.toml")

    config = KaiRpcApplicationConfig(
        processId=None,
        demo_mode=True,
        root_path=SAMPLE_APP_DIR,
        kai_backend_url=SERVER_URL,
        log_dir_path="./logs",
        model_provider=kai_config.models,
    )

    current_directory = Path(os.path.dirname(os.path.realpath(__file__)))
    rpc_binary_path = current_directory / ".." / "kai" / "rpc_server" / "main.py"
    rpc_subprocess = subprocess.Popen(  # trunk-ignore(bandit/B603,bandit/B607)
        ["python", rpc_binary_path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        # stderr=subprocess.PIPE,
        env=os.environ,
    )

    app = KaiRpcApplication()

    @app.add_notify(method="logMessage")
    def logMessage(
        app: KaiRpcApplication, server: JsonRpcServer, id: JsonRpcId, params: dict
    ):
        KAI_LOG.info(str(params))
        pass

    rpc_server = JsonRpcServer(
        json_rpc_stream=BareJsonStream(
            cast(BufferedReader, rpc_subprocess.stdout),
            cast(BufferedWriter, rpc_subprocess.stdin),
        ),
        app=app,
        request_timeout=240,
    )
    rpc_server.start()

    # wait for server to start up
    time.sleep(3)

    try:
        response = rpc_server.send_request(
            method="initialize", params=config.model_dump()
        )
        if response is None:
            raise Exception("Failed to initialize RPC server, received None response")
        elif isinstance(response, JsonRpcError):
            raise Exception(
                f"Failed to initialize RPC server - {response.code} {response.message}"
            )
        yield rpc_server
    finally:
        rpc_subprocess.terminate()
        rpc_subprocess.wait()
        rpc_server.stop()


def write_to_disk(file_path: Path, updated_file_contents: FileSolutionContent):
    file_path = str(file_path)  # Temporary fix for Path object

    # We expect that we are overwriting the file, so all directories should exist
    intended_file_path = f"{SAMPLE_APP_DIR}/{file_path}"
    if not os.path.exists(intended_file_path):
        KAI_LOG.warning(
            f"**WARNING* File {intended_file_path} does not exist.  Proceeding, but suspect this is a new file or there is a problem with the filepath"
        )

    KAI_LOG.info(f"Writing updated source code to {intended_file_path}")
    try:
        with open(intended_file_path, "w") as f:
            f.write(updated_file_contents.updated_file)
    except Exception as e:
        KAI_LOG.error(
            f"Failed to write updated_file @ {intended_file_path} with error: {e}"
        )
        KAI_LOG.error(f"Contents: {updated_file_contents.updated_file}")
        return

    # since the other files are all contained within the llm_result, avoid duplication
    # when they're available
    if updated_file_contents.llm_results:
        llm_result_path = f"{intended_file_path}.llm_results.md"
        KAI_LOG.info(f"Writing llm_result to {llm_result_path}")
        try:
            with open(llm_result_path, "w") as f:
                f.write("\n---\n".join(updated_file_contents.llm_results))
        except Exception as e:
            KAI_LOG.error(
                f"Failed to write llm_result @ {llm_result_path} with error: {e}"
            )
            KAI_LOG.error(f"LLM Results: {updated_file_contents.llm_results}")
            return
    else:
        reasoning_path = f"{intended_file_path}.reasoning"
        KAI_LOG.info(f"Writing reasoning to {reasoning_path}")
        try:
            with open(reasoning_path, "w") as f:
                json.dump(updated_file_contents.reasoning, f)
        except Exception as e:
            KAI_LOG.error(
                f"Failed to write reasoning @ {reasoning_path} with error: {e}"
            )
            KAI_LOG.error(f"Reasoning: {updated_file_contents.reasoning}")
            return

        additional_information_path = f"{intended_file_path}.additional_information.md"
        KAI_LOG.info(f"Writing additional_information to {additional_information_path}")
        try:
            with open(additional_information_path, "w") as f:
                f.write(updated_file_contents.additional_info)
        except Exception as e:
            KAI_LOG.error(
                f"Failed to write additional_information @ {additional_information_path} with error: {e}"
            )
            KAI_LOG.error(
                f"Additional information: {updated_file_contents.additional_info}"
            )
            return


def process_file(
    server: JsonRpcServer,
    file_path: Path,
    incidents: list[ExtendedIncident],
    num_impacted_files: int,
    count: int,
):
    start = time.time()
    KAI_LOG.info(
        f"File #{count} of {num_impacted_files} - Processing {file_path} which has {len(incidents)} incidents."
    )

    params = GetRagSolutionParams(
        file_path=file_path,
        incidents=incidents,
        include_solved_incidents=False,
        trace_enabled=True,
    )

    response = server.send_request("getRAGSolution", params.model_dump())

    if isinstance(response, JsonRpcError):
        return (
            f"Failed to generate fix for file {params.file_path} - {response.message}"
        )
    elif isinstance(response, JsonRpcResponse) and response.error is not None:
        return f"Failed to generate fix for file {params.file_path} - {response.error.code} {response.error.message}"

    try:
        updated_file_contents: dict = FileSolutionContent.model_validate(
            response.result
        )
    except Exception:
        return f"Invalid response received for file {params.file_path}"

    if os.getenv("WRITE_TO_DISK", "").lower() not in ("false", "0", "no"):
        write_to_disk(file_path, updated_file_contents)

    end = time.time()
    return f"Took {end-start}s to process {file_path} with {len(incidents)} violations"


def run_demo(report: Report, server: JsonRpcServer):
    impacted_files = report.get_impacted_files()
    num_impacted_files = len(impacted_files)

    total_incidents = sum(len(incidents) for incidents in impacted_files.values())
    print(f"{num_impacted_files} files with a total of {total_incidents} incidents.")

    for count, (file_path, incidents) in enumerate(impacted_files.items(), 1):
        process_file(
            server=server,
            incidents=incidents,
            file_path=file_path,
            count=count,
            num_impacted_files=num_impacted_files,
        )
        break

    # max_workers = int(os.environ.get("KAI_MAX_WORKERS", 8))
    # KAI_LOG.info(f"Running in parallel with {max_workers} workers")
    # with ThreadPoolExecutor(max_workers=max_workers) as executor:
    #     futures: list[Future[str]] = []
    #     for count, (file_path, incidents) in enumerate(impacted_files.items(), 1):
    #         future = executor.submit(
    #             process_file, server, file_path, incidents, num_impacted_files, count
    #         )
    #         futures.append(future)

    #     for future in as_completed(futures):
    #         try:
    #             result = future.result()
    #             KAI_LOG.info(f"Result:  {result}")
    #         except Exception as exc:
    #             KAI_LOG.error(f"Generated an exception: {exc}")
    #             KAI_LOG.error(traceback.format_exc())
    #             exit(1)

    #         remaining_files -= 1
    #         KAI_LOG.info(
    #             f"{remaining_files} files remaining from total of {num_impacted_files}"
    #         )


def main():
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(formatter)
    # logging.getLogger("jsonrpc").setLevel(logging.CRITICAL)
    KAI_LOG.addHandler(console_handler)
    KAI_LOG.setLevel(logging.DEBUG)

    start = time.time()

    coolstore_analysis_dir = "./analysis/coolstore/output.yaml"
    report = Report.load_report_from_file(coolstore_analysis_dir)
    try:
        with initialize_rpc_server() as server:
            run_demo(report, server)
        KAI_LOG.info(
            f"Total time to process '{coolstore_analysis_dir}' was {time.time()-start}s"
        )
    except Exception as e:
        KAI_LOG.error(f"Error running demo - {e}")


if __name__ == "__main__":
    main()
