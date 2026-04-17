from __future__ import annotations

import json
import queue
import shutil
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO, Callable
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .manifest import PlannerConfig, load_manifest, utc_now_iso
from .runner import RunCancelled, run_manifest
from .storage import write_json_atomic
from .workflow import plan_video_job


JOB_STATUS_UPLOADING = "uploading"
JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_CANCELLING = "cancelling"
JOB_STATUS_SUCCEEDED = "succeeded"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"
RECOVERABLE_STATUSES = {JOB_STATUS_QUEUED, JOB_STATUS_RUNNING}
TERMINAL_STATUSES = {JOB_STATUS_SUCCEEDED, JOB_STATUS_FAILED, JOB_STATUS_CANCELLED}
QUEUE_COUNTED_STATUSES = {JOB_STATUS_UPLOADING, JOB_STATUS_QUEUED}


class ServiceError(RuntimeError):
    pass


class RangeRequestError(ServiceError):
    pass


class QueueFullError(ServiceError):
    pass


class CancellationError(ServiceError):
    pass


class UploadCancelled(ServiceError):
    pass


@dataclass(frozen=True)
class ServiceConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    state_dir: str | Path = "service_state"
    scale: float = 2.0
    max_queued_jobs: int = 0
    planner: PlannerConfig = field(default_factory=PlannerConfig)
    upstream_root: str | Path | None = None
    infer_script: str | Path | None = None
    weights_dir: str | Path | None = None


@dataclass
class JobRecord:
    job_id: str
    status: str
    created_at: str
    updated_at: str
    input_filename: str
    input_size_bytes: int
    uploaded_size_bytes: int
    input_path: str
    output_path: str
    work_dir: str
    manifest_path: str
    scale: float
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    cancel_requested_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> "JobRecord":
        payload.setdefault("uploaded_size_bytes", payload.get("input_size_bytes", 0))
        return cls(**payload)


class JobStore:
    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.jobs_dir = self.state_dir / "jobs"
        self._lock = threading.Lock()
        self._active_uploads: set[str] = set()
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

    def _job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    def _job_json(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "job.json"

    def create_upload_job(
        self,
        *,
        filename: str,
        content_length: int,
        scale: float,
        max_queued_jobs: int = 0,
    ) -> JobRecord:
        if content_length <= 0:
            raise ServiceError("Uploaded video body is empty")

        job_id = uuid.uuid4().hex
        safe_filename = Path(filename).name or "input.mp4"
        suffix = Path(safe_filename).suffix or ".mp4"
        input_stem = Path(safe_filename).stem or "input"
        now = utc_now_iso()

        job_dir = self._job_dir(job_id)
        input_dir = job_dir / "input"
        result_dir = job_dir / "result"
        work_dir = job_dir / "work"
        input_path = input_dir / f"source{suffix}"
        output_path = result_dir / f"{input_stem}_x{scale:g}.mp4"
        manifest_path = work_dir / "manifest.json"

        with self._lock:
            try:
                if max_queued_jobs > 0 and self._count_jobs_unlocked(QUEUE_COUNTED_STATUSES) >= max_queued_jobs:
                    raise QueueFullError(f"Queue is full; max queued jobs is {max_queued_jobs}")
                input_dir.mkdir(parents=True, exist_ok=True)
                result_dir.mkdir(parents=True, exist_ok=True)
                work_dir.mkdir(parents=True, exist_ok=True)
                record = JobRecord(
                    job_id=job_id,
                    status=JOB_STATUS_UPLOADING,
                    created_at=now,
                    updated_at=now,
                    input_filename=safe_filename,
                    input_size_bytes=content_length,
                    uploaded_size_bytes=0,
                    input_path=str(input_path.resolve()),
                    output_path=str(output_path.resolve()),
                    work_dir=str(work_dir.resolve()),
                    manifest_path=str(manifest_path.resolve()),
                    scale=scale,
                )
                self.save(record)
                return record
            except Exception:
                shutil.rmtree(job_dir, ignore_errors=True)
                raise

    def receive_upload(
        self,
        job_id: str,
        *,
        stream: BinaryIO,
        content_length: int,
        keep_record_on_error: bool,
        should_stop: Callable[[], bool] | None = None,
    ) -> JobRecord:
        with self._lock:
            record = self.load(job_id)
            if record.status != JOB_STATUS_UPLOADING:
                raise ServiceError(f"Job is not accepting uploads: {record.status}")
            if content_length != record.input_size_bytes:
                raise ServiceError(
                    f"Upload Content-Length {content_length} does not match reserved size {record.input_size_bytes}"
                )
            self._active_uploads.add(job_id)

        input_path = Path(record.input_path)
        temporary_input_path = input_path.with_name(f"{input_path.name}.part")
        if input_path.exists() or record.uploaded_size_bytes >= record.input_size_bytes:
            self.finish_upload(job_id)
            raise ServiceError("Upload is already complete")

        try:
            _copy_exactly(
                stream,
                temporary_input_path,
                content_length,
                on_progress=lambda uploaded: self.update_upload_progress(job_id, uploaded),
                should_stop=should_stop,
            )
            if should_stop and should_stop():
                raise UploadCancelled("Upload cancellation requested")
            temporary_input_path.replace(input_path)
            if should_stop and should_stop():
                input_path.unlink(missing_ok=True)
                raise UploadCancelled("Upload cancellation requested")
        except Exception as exc:
            temporary_input_path.unlink(missing_ok=True)
            if isinstance(exc, UploadCancelled):
                cancelled_record = self.load(job_id)
                if cancelled_record.status != JOB_STATUS_CANCELLED:
                    self.update(
                        job_id,
                        status=JOB_STATUS_CANCELLED,
                        completed_at=utc_now_iso(),
                        cancel_requested_at=cancelled_record.cancel_requested_at or utc_now_iso(),
                        error=None,
                    )
                raise
            if keep_record_on_error:
                self.update(
                    job_id,
                    status=JOB_STATUS_FAILED,
                    completed_at=utc_now_iso(),
                    error=f"{type(exc).__name__}: {exc}",
                )
            else:
                self.delete_job(job_id)
            raise
        finally:
            self.finish_upload(job_id)

        return self.update(
            job_id,
            status=JOB_STATUS_QUEUED,
            uploaded_size_bytes=content_length,
            completed_at=None,
            error=None,
        )

    def save(self, record: JobRecord) -> None:
        write_json_atomic(self._job_json(record.job_id), record.to_dict(), ensure_ascii=False, indent=2)

    def load(self, job_id: str) -> JobRecord:
        job_json = self._job_json(job_id)
        if not job_json.exists():
            raise KeyError(job_id)
        return JobRecord.from_dict(json.loads(job_json.read_text(encoding="utf-8")))

    def update(self, job_id: str, **changes) -> JobRecord:
        with self._lock:
            record = self.load(job_id)
            for key, value in changes.items():
                setattr(record, key, value)
            record.updated_at = utc_now_iso()
            self.save(record)
            return record

    def update_upload_progress(self, job_id: str, uploaded_size_bytes: int) -> JobRecord:
        with self._lock:
            record = self.load(job_id)
            record.uploaded_size_bytes = min(
                record.input_size_bytes,
                max(record.uploaded_size_bytes, uploaded_size_bytes),
            )
            record.updated_at = utc_now_iso()
            self.save(record)
            return record

    def list_jobs(self) -> list[JobRecord]:
        records: list[JobRecord] = []
        for job_json in self.jobs_dir.glob("*/job.json"):
            try:
                records.append(JobRecord.from_dict(json.loads(job_json.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
        return sorted(records, key=lambda item: item.created_at, reverse=True)

    def _count_jobs_unlocked(self, statuses: set[str]) -> int:
        count = 0
        for job_json in self.jobs_dir.glob("*/job.json"):
            try:
                payload = json.loads(job_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
            if payload.get("status") in statuses:
                count += 1
        return count

    def cleanup_orphaned_jobs(self) -> None:
        with self._lock:
            for job_dir in self.jobs_dir.iterdir():
                if not job_dir.is_dir():
                    continue
                if (job_dir / "job.json").exists():
                    continue
                shutil.rmtree(job_dir, ignore_errors=True)

    def cleanup_partial_upload(self, job_id: str) -> None:
        try:
            record = self.load(job_id)
        except KeyError:
            return
        input_path = Path(record.input_path)
        temporary_input_path = input_path.with_name(f"{input_path.name}.part")
        temporary_input_path.unlink(missing_ok=True)

    def delete_job(self, job_id: str) -> None:
        with self._lock:
            shutil.rmtree(self._job_dir(job_id), ignore_errors=True)

    def is_upload_active(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._active_uploads

    def finish_upload(self, job_id: str) -> None:
        with self._lock:
            self._active_uploads.discard(job_id)


class JobManager:
    def __init__(self, config: ServiceConfig):
        self.config = config
        self.store = JobStore(config.state_dir)
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._stop_event = threading.Event()
        self._worker = threading.Thread(target=self._worker_loop, name="flashvsr-job-worker", daemon=True)

    def start(self) -> None:
        self.store.cleanup_orphaned_jobs()
        for record in self.store.list_jobs():
            if record.status == JOB_STATUS_UPLOADING:
                self.store.cleanup_partial_upload(record.job_id)
                self.store.update(
                    record.job_id,
                    status=JOB_STATUS_FAILED,
                    completed_at=utc_now_iso(),
                    error=record.error or "Upload was interrupted before completion",
                )
                continue
            if record.status == JOB_STATUS_CANCELLING:
                self.store.cleanup_partial_upload(record.job_id)
                self.store.update(
                    record.job_id,
                    status=JOB_STATUS_CANCELLED,
                    completed_at=utc_now_iso(),
                    cancel_requested_at=record.cancel_requested_at or utc_now_iso(),
                    error=None,
                )
                continue
            if record.status in RECOVERABLE_STATUSES:
                self._queue.put(record.job_id)
        self._worker.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop_event.set()
        self._queue.put(None)
        self._worker.join(timeout=timeout)

    def submit_upload(self, *, filename: str, content_length: int, stream: BinaryIO, scale: float | None = None) -> JobRecord:
        record = self.store.create_upload_job(
            filename=filename,
            content_length=content_length,
            scale=scale if scale is not None else self.config.scale,
            max_queued_jobs=self.config.max_queued_jobs,
        )
        record = self.store.receive_upload(
            record.job_id,
            stream=stream,
            content_length=content_length,
            keep_record_on_error=False,
            should_stop=lambda: self._upload_cancel_requested(record.job_id),
        )
        self._queue.put(record.job_id)
        return record

    def create_upload_session(self, *, filename: str, content_length: int, scale: float | None = None) -> JobRecord:
        return self.store.create_upload_job(
            filename=filename,
            content_length=content_length,
            scale=scale if scale is not None else self.config.scale,
            max_queued_jobs=self.config.max_queued_jobs,
        )

    def upload_to_job(self, job_id: str, *, content_length: int, stream: BinaryIO) -> JobRecord:
        record = self.store.receive_upload(
            job_id,
            stream=stream,
            content_length=content_length,
            keep_record_on_error=True,
            should_stop=lambda: self._upload_cancel_requested(job_id),
        )
        self._queue.put(record.job_id)
        return record

    def get_payload(self, job_id: str) -> dict:
        return self._payload_for_record(self.store.load(job_id))

    def list_payloads(self, *, limit: int = 50) -> dict:
        all_records = self.store.list_jobs()
        records = all_records[:limit]
        return {
            "count": len(records),
            "queue": {
                "max_queued_jobs": self.config.max_queued_jobs if self.config.max_queued_jobs > 0 else None,
                "uploading_jobs": sum(1 for record in all_records if record.status == JOB_STATUS_UPLOADING),
                "queued_jobs": sum(1 for record in all_records if record.status == JOB_STATUS_QUEUED),
            },
            "items": [self._payload_for_record(record) for record in records],
        }

    def cancel_job(self, job_id: str) -> JobRecord:
        record = self.store.load(job_id)
        if record.status == JOB_STATUS_CANCELLED:
            return record
        if record.status == JOB_STATUS_UPLOADING:
            timestamp = record.cancel_requested_at or utc_now_iso()
            if self.store.is_upload_active(job_id):
                return self.store.update(
                    job_id,
                    status=JOB_STATUS_CANCELLING,
                    cancel_requested_at=timestamp,
                    completed_at=None,
                    error=None,
                )
            self.store.cleanup_partial_upload(job_id)
            return self.store.update(
                job_id,
                status=JOB_STATUS_CANCELLED,
                completed_at=timestamp,
                cancel_requested_at=timestamp,
                error=None,
            )
        if record.status in {JOB_STATUS_SUCCEEDED, JOB_STATUS_FAILED}:
            raise CancellationError(f"Job is already complete: {record.status}")

        timestamp = utc_now_iso()
        if record.status == JOB_STATUS_QUEUED:
            return self.store.update(
                job_id,
                status=JOB_STATUS_CANCELLED,
                completed_at=timestamp,
                cancel_requested_at=timestamp,
                error=None,
            )
        if record.status in {JOB_STATUS_RUNNING, JOB_STATUS_CANCELLING}:
            return self.store.update(
                job_id,
                status=JOB_STATUS_CANCELLING,
                cancel_requested_at=record.cancel_requested_at or timestamp,
                completed_at=None,
                error=None,
            )
        raise CancellationError(f"Job cannot be cancelled from status: {record.status}")

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            job_id = self._queue.get()
            try:
                if job_id is None:
                    return
                self._process_job(job_id)
            finally:
                self._queue.task_done()

    def _process_job(self, job_id: str) -> None:
        try:
            record = self.store.load(job_id)
        except KeyError:
            return
        if record.status not in RECOVERABLE_STATUSES:
            return

        started_at = record.started_at or utc_now_iso()
        self.store.update(job_id, status=JOB_STATUS_RUNNING, started_at=started_at, completed_at=None, error=None)

        try:
            manifest_path = Path(record.manifest_path)
            if not manifest_path.exists():
                plan_video_job(
                    input_path=record.input_path,
                    output_path=record.output_path,
                    work_dir=record.work_dir,
                    scale=record.scale,
                    planner=self.config.planner,
                    manifest_path=record.manifest_path,
                    upstream_root=self.config.upstream_root,
                    infer_script=self.config.infer_script,
                    weights_dir=self.config.weights_dir,
                )
            if self._job_cancel_requested(job_id):
                raise RunCancelled("Job cancellation requested")
            run_manifest(
                record.manifest_path,
                upstream_root=self.config.upstream_root,
                infer_script=self.config.infer_script,
                weights_dir=self.config.weights_dir,
                resume=True,
                should_stop=lambda: self._job_cancel_requested(job_id),
            )
        except RunCancelled:
            cancelled_record = self.store.load(job_id)
            self.store.update(
                job_id,
                status=JOB_STATUS_CANCELLED,
                completed_at=utc_now_iso(),
                cancel_requested_at=cancelled_record.cancel_requested_at or utc_now_iso(),
                error=None,
            )
            return
        except Exception as exc:
            self.store.update(
                job_id,
                status=JOB_STATUS_FAILED,
                completed_at=utc_now_iso(),
                error=f"{type(exc).__name__}: {exc}",
            )
            return

        self.store.update(job_id, status=JOB_STATUS_SUCCEEDED, completed_at=utc_now_iso(), error=None)

    def _job_cancel_requested(self, job_id: str) -> bool:
        try:
            record = self.store.load(job_id)
        except KeyError:
            return False
        return record.status == JOB_STATUS_CANCELLING

    def _upload_cancel_requested(self, job_id: str) -> bool:
        try:
            record = self.store.load(job_id)
        except KeyError:
            return True
        return record.status in {JOB_STATUS_CANCELLING, JOB_STATUS_CANCELLED}

    def _payload_for_record(self, record: JobRecord) -> dict:
        output_path = Path(record.output_path)
        result_ready = record.status == JOB_STATUS_SUCCEEDED and output_path.exists()
        upload_percent = _ratio_percent(record.uploaded_size_bytes, record.input_size_bytes)
        payload = {
            "job_id": record.job_id,
            "status": record.status,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "started_at": record.started_at,
            "completed_at": record.completed_at,
            "cancel_requested_at": record.cancel_requested_at,
            "scale": record.scale,
            "input": {
                "filename": record.input_filename,
                "size_bytes": record.input_size_bytes,
                "uploaded_bytes": record.uploaded_size_bytes,
                "upload_percent": upload_percent,
            },
            "error": record.error,
        }
        payload["urls"] = {
            "status": f"/v1/jobs/{record.job_id}",
            "result": f"/v1/jobs/{record.job_id}/result",
            "upload": f"/v1/jobs/{record.job_id}/upload" if record.status == JOB_STATUS_UPLOADING else None,
        }
        payload["result"] = {
            "ready": result_ready,
            "download_url": f"/v1/jobs/{record.job_id}/result" if result_ready else None,
            "filename": output_path.name if result_ready else None,
            "size_bytes": output_path.stat().st_size if result_ready else None,
            "accept_ranges": result_ready,
        }
        if record.status == JOB_STATUS_UPLOADING:
            payload["progress"] = _with_upload_progress(
                _upload_progress(record),
                uploaded_size_bytes=record.uploaded_size_bytes,
                input_size_bytes=record.input_size_bytes,
            )
        else:
            payload["progress"] = _with_upload_progress(
                _manifest_progress(
                    record.manifest_path,
                    job_status=record.status,
                    started_at=record.started_at,
                    completed_at=record.completed_at,
                ),
                uploaded_size_bytes=record.uploaded_size_bytes,
                input_size_bytes=record.input_size_bytes,
            )
        return payload


class FlashVSRHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], manager: JobManager):
        super().__init__(server_address, FlashVSRRequestHandler)
        self.manager = manager


class FlashVSRRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def manager(self) -> JobManager:
        return self.server.manager  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path in {"/healthz", "/v1/healthz"}:
            self._send_json({"status": "ok"})
            return
        if path == "/v1/jobs":
            params = parse_qs(parsed.query)
            limit = _parse_int(params.get("limit", ["50"])[0], default=50)
            self._send_json(self.manager.list_payloads(limit=limit))
            return
        if path.startswith("/v1/jobs/"):
            parts = path.split("/")
            if len(parts) == 4:
                self._handle_job_status(parts[3])
                return
            if len(parts) == 5 and parts[4] == "result":
                self._handle_job_result(parts[3])
                return
        self._send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path != "/v1/jobs":
            self._send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type == "application/json":
            self._handle_job_create(parsed.query)
            return
        self._handle_job_submit(parsed.query)

    def do_PUT(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path.startswith("/v1/jobs/"):
            parts = path.split("/")
            if len(parts) == 5 and parts[4] == "upload":
                self._handle_job_upload(parts[3])
                return
        self._send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_DELETE(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path.startswith("/v1/jobs/"):
            parts = path.split("/")
            if len(parts) == 4:
                self._handle_job_cancel(parts[3])
                return
        self._send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format: str, *args) -> None:
        return

    def _handle_job_status(self, job_id: str) -> None:
        try:
            payload = self.manager.get_payload(job_id)
        except KeyError:
            self._send_error(HTTPStatus.NOT_FOUND, f"Job not found: {job_id}")
            return
        self._send_json(payload)

    def _handle_job_result(self, job_id: str) -> None:
        try:
            record = self.manager.store.load(job_id)
        except KeyError:
            self._send_error(HTTPStatus.NOT_FOUND, f"Job not found: {job_id}")
            return
        if record.status != JOB_STATUS_SUCCEEDED:
            self._send_error(HTTPStatus.CONFLICT, f"Job is not complete: {record.status}")
            return
        output_path = Path(record.output_path)
        if not output_path.exists():
            self._send_error(HTTPStatus.NOT_FOUND, "Result file is missing")
            return
        file_size = output_path.stat().st_size
        range_header = self.headers.get("Range")
        try:
            requested_range = _parse_byte_range(range_header, file_size)
        except RangeRequestError as exc:
            self._send_error(
                HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                str(exc),
                headers={"Content-Range": f"bytes */{file_size}"},
            )
            return
        start = 0
        end = file_size - 1
        response_status = HTTPStatus.OK
        if requested_range is not None:
            start, end = requested_range
            response_status = HTTPStatus.PARTIAL_CONTENT

        self.send_response(response_status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(_byte_range_length(start, end)))
        self.send_header("Content-Disposition", _content_disposition_attachment(output_path.name))
        if requested_range is not None:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()
        try:
            _stream_file_range(output_path, self.wfile, start=start, end=end)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _handle_job_cancel(self, job_id: str) -> None:
        try:
            record = self.manager.cancel_job(job_id)
        except KeyError:
            self._send_error(HTTPStatus.NOT_FOUND, f"Job not found: {job_id}")
            return
        except CancellationError as exc:
            self._send_error(HTTPStatus.CONFLICT, str(exc))
            return

        status = HTTPStatus.ACCEPTED if record.status == JOB_STATUS_CANCELLING else HTTPStatus.OK
        self._send_json(self.manager.get_payload(job_id), status=status)

    def _handle_job_create(self, query: str) -> None:
        content_length_header = self.headers.get("Content-Length")
        if content_length_header is None:
            self._send_error(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required")
            return
        try:
            body_length = int(content_length_header)
        except ValueError:
            self._send_error(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return
        try:
            payload = json.loads(self.rfile.read(max(body_length, 0)).decode("utf-8") if body_length > 0 else "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON body")
            return

        params = parse_qs(query)
        filename = payload.get("filename") or params.get("filename", ["input.mp4"])[0]
        filename = unquote(str(filename))
        size_bytes = payload.get("size_bytes", payload.get("content_length"))
        if size_bytes is None:
            self._send_error(HTTPStatus.BAD_REQUEST, "size_bytes is required")
            return
        try:
            content_length = int(size_bytes)
        except (TypeError, ValueError):
            self._send_error(HTTPStatus.BAD_REQUEST, "size_bytes must be an integer")
            return
        try:
            scale = _parse_scale(str(payload.get("scale", params.get("scale", [str(self.manager.config.scale)])[0])))
            record = self.manager.create_upload_session(
                filename=filename,
                content_length=content_length,
                scale=scale,
            )
        except QueueFullError as exc:
            self._send_error(HTTPStatus.TOO_MANY_REQUESTS, str(exc))
            return
        except ServiceError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return

        self._send_json(self.manager.get_payload(record.job_id), status=HTTPStatus.CREATED)

    def _handle_job_submit(self, query: str) -> None:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/octet-stream":
            self._send_error(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "Submit videos as application/octet-stream with X-Filename or ?filename=...",
            )
            return

        content_length_header = self.headers.get("Content-Length")
        if not content_length_header:
            self._send_error(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required")
            return
        try:
            content_length = int(content_length_header)
        except ValueError:
            self._send_error(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return

        params = parse_qs(query)
        filename = self.headers.get("X-Filename") or params.get("filename", ["input.mp4"])[0]
        filename = unquote(filename)
        try:
            scale = _parse_scale(params.get("scale", [str(self.manager.config.scale)])[0])
            record = self.manager.submit_upload(
                filename=filename,
                content_length=content_length,
                stream=self.rfile,
                scale=scale,
            )
        except UploadCancelled as exc:
            self._send_error(HTTPStatus.CONFLICT, str(exc))
            return
        except QueueFullError as exc:
            self._send_error(HTTPStatus.TOO_MANY_REQUESTS, str(exc))
            return
        except ServiceError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return

        self._send_json(self.manager.get_payload(record.job_id), status=HTTPStatus.ACCEPTED)

    def _handle_job_upload(self, job_id: str) -> None:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/octet-stream":
            self._send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Upload body must be application/octet-stream")
            return

        content_length_header = self.headers.get("Content-Length")
        if not content_length_header:
            self._send_error(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required")
            return
        try:
            content_length = int(content_length_header)
        except ValueError:
            self._send_error(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return

        try:
            record = self.manager.upload_to_job(
                job_id,
                content_length=content_length,
                stream=self.rfile,
            )
        except UploadCancelled as exc:
            self._send_error(HTTPStatus.CONFLICT, str(exc))
            return
        except KeyError:
            self._send_error(HTTPStatus.NOT_FOUND, f"Job not found: {job_id}")
            return
        except ServiceError as exc:
            self._send_error(HTTPStatus.CONFLICT, str(exc))
            return

        self._send_json(self.manager.get_payload(record.job_id), status=HTTPStatus.ACCEPTED)

    def _send_json(self, payload: dict, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: HTTPStatus, message: str, *, headers: dict[str, str] | None = None) -> None:
        body = json.dumps({"error": message}, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _copy_exactly(
    source: BinaryIO,
    destination: Path,
    byte_count: int,
    *,
    on_progress: Callable[[int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> None:
    remaining = byte_count
    copied = 0
    with destination.open("wb") as handle:
        while remaining > 0:
            if should_stop is not None and should_stop():
                raise UploadCancelled("Upload cancellation requested")
            chunk = source.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ServiceError("Upload ended before Content-Length bytes were received")
            handle.write(chunk)
            remaining -= len(chunk)
            copied += len(chunk)
            if on_progress is not None:
                on_progress(copied)


def _stream_file_range(path: str | Path, sink: BinaryIO, *, start: int, end: int) -> None:
    remaining = _byte_range_length(start, end)
    with Path(path).open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            sink.write(chunk)
            remaining -= len(chunk)


def _byte_range_length(start: int, end: int) -> int:
    if end < start:
        return 0
    return (end - start) + 1


def _parse_byte_range(range_header: str | None, file_size: int) -> tuple[int, int] | None:
    if not range_header:
        return None
    if file_size <= 0:
        raise RangeRequestError("Range request is not valid for an empty result")
    unit, separator, value = range_header.partition("=")
    if separator != "=" or unit.strip().lower() != "bytes":
        raise RangeRequestError("Only single byte ranges are supported")
    if "," in value:
        raise RangeRequestError("Multiple byte ranges are not supported")

    start_text, dash, end_text = value.strip().partition("-")
    if dash != "-":
        raise RangeRequestError("Invalid Range header")

    try:
        if start_text == "":
            suffix_length = int(end_text)
            if suffix_length <= 0:
                raise RangeRequestError("Invalid Range header")
            start = max(file_size - suffix_length, 0)
            end = file_size - 1
        else:
            start = int(start_text)
            end = (file_size - 1) if end_text == "" else int(end_text)
    except ValueError as exc:
        raise RangeRequestError("Invalid Range header") from exc

    if start < 0 or end < 0 or start >= file_size or end < start:
        raise RangeRequestError("Requested byte range is outside the result size")
    end = min(end, file_size - 1)
    return start, end


def _content_disposition_attachment(filename: str) -> str:
    sanitized = "".join(character if 32 <= ord(character) < 127 else "_" for character in Path(filename).name)
    fallback = sanitized.replace("\\", "_").replace('"', "_") or "download"
    suffix = Path(filename).suffix
    if suffix and not fallback.endswith(suffix):
        fallback = f"{Path(fallback).stem or 'download'}{suffix}"
    encoded = quote(
        "".join(character if character not in "\r\n" else "_" for character in Path(filename).name),
        safe="!#$&+-.^_`|~",
    )
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


def _parse_scale(value: str) -> float:
    try:
        scale = float(value)
    except ValueError as exc:
        raise ServiceError("scale must be a number") from exc
    if scale <= 0:
        raise ServiceError("scale must be positive")
    return scale


def _parse_int(value: str, *, default: int) -> int:
    try:
        parsed = int(value)
    except ValueError:
        return default
    return max(parsed, 1)


def _ratio_percent(done: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round((done / total) * 100, 2)


def _upload_progress(record: JobRecord) -> dict:
    elapsed_seconds = _elapsed_seconds(record.created_at)
    upload_percent = _ratio_percent(record.uploaded_size_bytes, record.input_size_bytes)
    estimated_remaining_seconds = None
    if (
        elapsed_seconds is not None
        and record.uploaded_size_bytes > 0
        and record.uploaded_size_bytes < record.input_size_bytes
    ):
        remaining_bytes = record.input_size_bytes - record.uploaded_size_bytes
        estimated_remaining_seconds = round(
            elapsed_seconds * remaining_bytes / record.uploaded_size_bytes,
            2,
        )
    return {
        "phase": "uploading",
        "total_chunks": None,
        "done_chunks": 0,
        "running_chunks": 0,
        "pending_chunks": 0,
        "failed_chunks": 0,
        "total_source_frames": None,
        "done_source_frames": 0,
        "percent": upload_percent,
        "elapsed_seconds": elapsed_seconds,
        "estimated_remaining_seconds": estimated_remaining_seconds,
        "current_chunk": None,
    }


def _with_upload_progress(progress: dict, *, uploaded_size_bytes: int, input_size_bytes: int) -> dict:
    progress["uploaded_bytes"] = uploaded_size_bytes
    progress["total_upload_bytes"] = input_size_bytes
    progress["upload_percent"] = _ratio_percent(uploaded_size_bytes, input_size_bytes)
    return progress


def _manifest_progress(
    manifest_path: str | Path,
    *,
    job_status: str,
    started_at: str | None = None,
    completed_at: str | None = None,
) -> dict:
    elapsed_seconds = _elapsed_seconds(started_at, completed_at)
    path = Path(manifest_path)
    if not path.exists():
        return {
            "phase": _phase_without_manifest(job_status),
            "total_chunks": None,
            "done_chunks": 0,
            "running_chunks": 0,
            "pending_chunks": 0,
            "failed_chunks": 0,
            "total_source_frames": None,
            "done_source_frames": 0,
            "percent": 100.0 if job_status == JOB_STATUS_SUCCEEDED else 0.0,
            "elapsed_seconds": elapsed_seconds,
            "estimated_remaining_seconds": 0 if job_status == JOB_STATUS_SUCCEEDED else None,
            "current_chunk": None,
        }
    try:
        manifest = load_manifest(path)
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return {
            "phase": _phase_without_manifest(job_status),
            "total_chunks": None,
            "done_chunks": 0,
            "running_chunks": 0,
            "pending_chunks": 0,
            "failed_chunks": 0,
            "total_source_frames": None,
            "done_source_frames": 0,
            "percent": 100.0 if job_status == JOB_STATUS_SUCCEEDED else 0.0,
            "elapsed_seconds": elapsed_seconds,
            "estimated_remaining_seconds": 0 if job_status == JOB_STATUS_SUCCEEDED else None,
            "current_chunk": None,
        }

    statuses = [chunk.status for chunk in manifest.chunks]
    total_source_frames = sum(chunk.source_length for chunk in manifest.chunks)
    done_chunks = [chunk for chunk in manifest.chunks if chunk.status == "done"]
    running_chunk = next((chunk for chunk in manifest.chunks if chunk.status == "running"), None)
    done_source_frames = sum(chunk.source_length for chunk in done_chunks)
    if job_status == JOB_STATUS_SUCCEEDED:
        done_source_frames = total_source_frames

    percent = 0.0
    if total_source_frames:
        percent = round((done_source_frames / total_source_frames) * 100, 2)
    if job_status == JOB_STATUS_SUCCEEDED:
        percent = 100.0

    estimated_remaining_seconds = None
    if job_status == JOB_STATUS_SUCCEEDED:
        estimated_remaining_seconds = 0
    elif (
        job_status not in {JOB_STATUS_FAILED, JOB_STATUS_CANCELLING, JOB_STATUS_CANCELLED}
        and elapsed_seconds is not None
        and done_source_frames > 0
        and done_source_frames < total_source_frames
    ):
        remaining_frames = total_source_frames - done_source_frames
        estimated_remaining_seconds = round(elapsed_seconds * remaining_frames / done_source_frames, 2)

    return {
        "phase": _progress_phase(job_status, statuses),
        "total_chunks": len(statuses),
        "done_chunks": len(done_chunks),
        "running_chunks": statuses.count("running"),
        "pending_chunks": statuses.count("pending"),
        "failed_chunks": statuses.count("failed"),
        "total_source_frames": total_source_frames,
        "done_source_frames": done_source_frames,
        "percent": percent,
        "elapsed_seconds": elapsed_seconds,
        "estimated_remaining_seconds": estimated_remaining_seconds,
        "current_chunk": (
            {
                "index": running_chunk.index,
                "source_start": running_chunk.source_start,
                "source_end": running_chunk.source_end,
                "source_length": running_chunk.source_length,
                "render_start": running_chunk.render_start,
                "render_end": running_chunk.render_end,
            }
            if running_chunk is not None
            else None
        ),
    }


def _phase_without_manifest(job_status: str) -> str:
    if job_status == JOB_STATUS_UPLOADING:
        return "uploading"
    if job_status == JOB_STATUS_RUNNING:
        return "planning"
    if job_status == JOB_STATUS_CANCELLING:
        return "cancelling"
    if job_status == JOB_STATUS_SUCCEEDED:
        return "completed"
    if job_status == JOB_STATUS_CANCELLED:
        return "cancelled"
    if job_status == JOB_STATUS_FAILED:
        return "failed"
    return "queued"


def _progress_phase(job_status: str, statuses: list[str]) -> str:
    if job_status == JOB_STATUS_CANCELLING:
        return "cancelling"
    if job_status == JOB_STATUS_CANCELLED:
        return "cancelled"
    if job_status == JOB_STATUS_FAILED:
        return "failed"
    if job_status == JOB_STATUS_SUCCEEDED:
        return "completed"
    if "running" in statuses:
        return "rendering"
    if statuses and all(status == "done" for status in statuses):
        return "finalizing"
    if any(status == "done" for status in statuses):
        return "rendering"
    return "queued"


def _elapsed_seconds(started_at: str | None, completed_at: str | None = None) -> float | None:
    if not started_at:
        return None
    try:
        started = datetime.fromisoformat(started_at)
        finished = datetime.fromisoformat(completed_at) if completed_at else datetime.now(timezone.utc)
    except ValueError:
        return None
    elapsed = (finished - started).total_seconds()
    return round(max(elapsed, 0.0), 2)


def serve(config: ServiceConfig) -> None:
    manager = JobManager(config)
    manager.start()
    server = FlashVSRHTTPServer((config.host, config.port), manager)
    print(f"flashvsr async service listening on http://{config.host}:{config.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        manager.stop()
