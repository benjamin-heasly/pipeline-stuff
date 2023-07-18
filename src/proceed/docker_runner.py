import logging
from typing import Union, Any
from datetime import datetime, timezone
from pathlib import Path
import docker
from docker.models.containers import Container
from docker.errors import DockerException, APIError
from proceed.model import Pipeline, ExecutionRecord, Step, StepResult, Timing
from proceed.file_matching import count_matches, match_patterns_in_dirs


def run_pipeline(
        original: Pipeline,
        execution_path: Path,
        args: dict[str, str] = {},
        force_rerun: bool = False,
        step_names: list[str] = None,
        client_kwargs: dict[str, Any] = {}
) -> ExecutionRecord:
    """
    Run steps of a pipeline and return results.

    :param original: a Pipeline, as read from an input YAML spec
    :return: a summary of Pipeline execution results.

    """

    logging.info("Starting pipeline run.")

    start = datetime.now(timezone.utc)

    amended = original._with_args_applied(args)._with_prototype_applied()
    step_results = []
    for step in amended.steps:
        if step_names and not step.name in step_names:
            logging.info(f"Ignoring step '{step.name}', not in list of steps to run: {step_names}")
            continue

        log_stem = step.name.replace(" ", "_")
        log_path = Path(execution_path, f"{log_stem}.log")
        step_result = run_step(step, log_path, force_rerun, client_kwargs)
        step_results.append(step_result)
        if step_result.exit_code:
            logging.error("Stopping pipeline run after error.")
            break

    finish = datetime.now(timezone.utc)
    duration = finish - start

    logging.info("Finished pipeline run.")

    return ExecutionRecord(
        original=original,
        amended=amended,
        step_results=step_results,
        timing=Timing(start.isoformat(sep="T"), finish.isoformat(sep="T"), duration.total_seconds())
    )


def run_step(
    step: Step,
    log_path: Path,
    force_rerun: bool = False,
    client_kwargs: dict[str, Any] = {}
) -> StepResult:
    logging.info(f"Step '{step.name}': starting.")

    start = datetime.now(timezone.utc)

    volume_dirs = step.volumes.keys()
    files_done = match_patterns_in_dirs(volume_dirs, step.match_done)
    if files_done:
        logging.info(f"Step '{step.name}': found {count_matches(files_done)} done files.")

        if force_rerun:
            logging.info(f"Step '{step.name}': executing despite done files because force_rerun is {force_rerun}.")
        else:
            logging.info(f"Step '{step.name}': skipping execution because done files were found.")
            return StepResult(
                name=step.name,
                skipped=True,
                files_done=files_done,
                timing=Timing(start.isoformat(sep="T"))
            )

    files_in = match_patterns_in_dirs(volume_dirs, step.match_in)
    logging.info(f"Step '{step.name}': found {count_matches(files_in)} input files.")

    (container, exit_code, exception) = run_container(step, log_path, client_kwargs)

    if exception is not None:
        # The container completed with an error.
        error_type_name = type(exception).__name__
        if isinstance(exception, APIError):
            error_message = f"{error_type_name}: {exception.explanation}\n"
        else:
            error_message = f"{error_type_name}: {exception.args}\n"

        with open(log_path, 'a') as f:
            f.write(error_message)

        logging.error(f"Step '{step.name}': error (see stack trace above) {error_message}")
        return StepResult(
            name=step.name,
            log_file=log_path.as_posix(),
            timing=Timing(start.isoformat(sep="T")),
            exit_code=exit_code
        )

    # It seems the container completed OK.
    files_out = match_patterns_in_dirs(volume_dirs, step.match_out)
    logging.info(f"Step '{step.name}': found {count_matches(files_out)} output files.")

    files_summary = match_patterns_in_dirs(volume_dirs, step.match_summary)
    logging.info(f"Step '{step.name}': found {count_matches(files_summary)} summary files.")

    finish = datetime.now(timezone.utc)
    duration = finish - start

    logging.info(f"Step '{step.name}': finished.")

    return StepResult(
        name=step.name,
        image_id=container.image.id,
        exit_code=exit_code,
        log_file=log_path.as_posix(),
        files_done=files_done,
        files_in=files_in,
        files_out=files_out,
        files_summary=files_summary,
        timing=Timing(start.isoformat(sep="T"), finish.isoformat(sep="T"), duration.total_seconds()),
    )


def run_container(
    step: Step,
    log_path: Path,
    client_kwargs: dict[str, Any] = {},
    max_attempts: int = 3
) -> tuple[Container, int, Exception]:
    retried_exception = None
    attempts = 0
    while attempts < max_attempts:
        try:
            device_requests = []
            if step.gpus:
                # This is roughly equivalent to the "--gpus" in "docker run --gpus ...".
                device_requests.append(docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]]))

            client = docker.from_env(**client_kwargs)
            container = client.containers.run(
                step.image,
                command=step.command,
                environment=step.environment,
                device_requests=device_requests,
                network_mode=step.network_mode,
                mac_address=step.mac_address,
                volumes=volumes_to_absolute_host(volumes_to_dictionaries(step.volumes)),
                working_dir=step.working_dir,
                auto_remove=False,
                remove=False,
                detach=True
            )
            logging.info(f"Container '{step.name}': waiting for process to complete.")

            # Tail the container logs and write new lines to the step log and the proceed log as they arrive.
            step_log_stream = container.logs(stdout=True, stderr=True, stream=True)
            with open(log_path, 'w') as f:
                for log_entry in step_log_stream:
                    log = log_entry.decode("utf-8")
                    f.write(log)
                    logging.info(f"Step '{step.name}': {log.strip()}")

            # Collect overall logs and status of the finished procedss.
            run_results = container.wait()
            exit_code = run_results['StatusCode']
            logging.info(f"Container '{step.name}': process completed with exit code {exit_code}")

            container.remove()

            return (container, exit_code, None)

        except APIError as api_error:
            if api_error.is_client_error():
                # Client errors are not worth retrying, just fail out.
                logging.error(f"Container had a Docker client error.", exc_info=True)
                return (None, -1, api_error)
            else:
                # Server errors might be transient and are worth retrying.
                logging.error(f"Container had a Docker server error, will retry.", exc_info=True)
                retried_exception = api_error

        except DockerException as docker_exception:
            # The other DockerExceptions besides APIError are probably not worth retrying, so just fail out.
            # https://github.com/docker/docker-py/blob/main/docker/errors.py
            logging.error(f"Container had a Docker error.", exc_info=True)
            return (None, -1, docker_exception)

        except Exception as unexpected_exception:
            # Other exceptions besides DockerException are unexpected!
            # But we have seen OSError here, for one.
            # Some of these seem to be transient, so we can retry them.
            logging.error(f"Container had an unexpected, non-Docker error, will retry", exc_info=True)
            retried_exception = unexpected_exception

        attempts += 1
        retry_log_message = f"Container attempts/retries at {attempts} out of {max_attempts}.\n"
        with open(log_path, 'a') as f:
            f.write(retry_log_message)
        logging.info(retry_log_message.strip())

    # If we got here it means we exhausted max_attempts, so we expect retried_exception to be filed in.
    return (None, -1, retried_exception)


def volumes_to_dictionaries(volumes: dict[str, Union[str, dict[str, str]]],
                            default_mode: str = "rw") -> dict[str, dict[str, str]]:
    normalized = {}
    for k, v in volumes.items():
        if isinstance(v, str):
            normalized[k] = {"bind": v, "mode": default_mode}
        else:
            normalized[k] = v
    return normalized


def volumes_to_absolute_host(volumes: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    absolute = {Path(k).absolute().as_posix(): v for k, v in volumes.items()}
    return absolute
