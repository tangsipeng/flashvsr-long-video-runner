from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from flashvsr_long_video_runner import service
from flashvsr_long_video_runner.manifest import PlannerConfig, UpstreamConfig, VideoMeta, build_manifest, load_manifest, save_manifest
from flashvsr_long_video_runner.planning import plan_chunks
from flashvsr_long_video_runner.service import FlashVSRHTTPServer, JobManager, JobRecord, JobStore, ServiceConfig, ServiceError


def _fake_plan_video_job(
    *,
    input_path,
    output_path,
    work_dir,
    scale,
    planner,
    manifest_path,
    upstream_root=None,
    infer_script=None,
    weights_dir=None,
):
    video = VideoMeta(
        width=16,
        height=16,
        total_frames=5,
        duration_seconds=1.0,
        fps_text="5/1",
        fps_float=5.0,
        has_audio=False,
    )
    manifest = build_manifest(
        input_path=Path(input_path),
        output_path=Path(output_path),
        work_dir=Path(work_dir),
        scale=scale,
        video=video,
        planner=planner,
        chunks=plan_chunks(
            video.total_frames,
            max_render_frames=planner.max_render_frames,
            tiny_tail_threshold=planner.tiny_tail_threshold,
            tail_merge_min_render_frames=planner.tail_merge_min_render_frames,
        ),
        upstream=UpstreamConfig(infer_script=str(infer_script or "fake_infer.py"), weights_dir=str(weights_dir or "fake_weights")),
    )
    save_manifest(manifest, manifest_path)
    return manifest


def _fake_run_manifest(manifest_path, **kwargs):
    manifest = load_manifest(manifest_path)
    for chunk in manifest.chunks:
        chunk.status = "done"
    output = Path(manifest.output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"fake-mp4")
    manifest.merged_video_path = str(output)
    save_manifest(manifest, manifest_path)
    return manifest


def _install_fake_runner(monkeypatch):
    monkeypatch.setattr(service, "plan_video_job", _fake_plan_video_job)
    monkeypatch.setattr(service, "run_manifest", _fake_run_manifest)


def _make_cancellable_fake_run(started_event: threading.Event):
    def _fake_run(manifest_path, *, should_stop=None, **kwargs):
        manifest = load_manifest(manifest_path)
        manifest.chunks[0].status = "running"
        save_manifest(manifest, manifest_path)
        started_event.set()
        deadline = time.time() + 5
        while time.time() < deadline:
            if should_stop and should_stop():
                raise service.RunCancelled("Job cancellation requested")
            time.sleep(0.02)
        raise AssertionError("cancel signal was not observed by fake runner")

    return _fake_run


class BlockingUploadStream:
    def __init__(self, payload: bytes, *, first_chunk_size: int):
        self.payload = payload
        self.first_chunk_size = first_chunk_size
        self.offset = 0
        self.read_count = 0
        self.first_chunk_sent = threading.Event()
        self.allow_remaining_reads = threading.Event()

    def read(self, size: int = -1) -> bytes:
        if self.offset >= len(self.payload):
            return b""
        if self.read_count == 0:
            chunk_size = self.first_chunk_size
            chunk = self.payload[self.offset : self.offset + chunk_size]
            self.offset += len(chunk)
            self.read_count += 1
            self.first_chunk_sent.set()
            return chunk
        self.allow_remaining_reads.wait(timeout=5)
        chunk_size = len(self.payload) - self.offset if size < 0 else min(size, len(self.payload) - self.offset)
        chunk = self.payload[self.offset : self.offset + chunk_size]
        self.offset += len(chunk)
        self.read_count += 1
        return chunk


def _wait_for_status(manager: JobManager, job_id: str, status: str) -> dict:
    deadline = time.time() + 5
    last_payload = {}
    while time.time() < deadline:
        last_payload = manager.get_payload(job_id)
        if last_payload["status"] == status:
            return last_payload
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not reach {status}; last payload={last_payload}")


def _write_progress_manifest(manifest_path: Path, *, total_frames: int = 50) -> None:
    video = VideoMeta(
        width=16,
        height=16,
        total_frames=total_frames,
        duration_seconds=10.0,
        fps_text="5/1",
        fps_float=5.0,
        has_audio=False,
    )
    planner = PlannerConfig()
    manifest = build_manifest(
        input_path=manifest_path.parent / "input.mp4",
        output_path=manifest_path.parent / "output.mp4",
        work_dir=manifest_path.parent,
        scale=2.0,
        video=video,
        planner=planner,
        chunks=plan_chunks(
            video.total_frames,
            max_render_frames=planner.max_render_frames,
            tiny_tail_threshold=planner.tiny_tail_threshold,
            tail_merge_min_render_frames=planner.tail_merge_min_render_frames,
        ),
    )
    manifest.chunks[0].status = "done"
    manifest.chunks[1].status = "running"
    save_manifest(manifest, manifest_path)


def test_job_store_cleans_up_partial_upload_failure(tmp_path: Path):
    store = JobStore(tmp_path)
    record = store.create_upload_job(
        filename="clip.mp4",
        content_length=20,
        scale=2.0,
    )

    with pytest.raises(ServiceError):
        store.receive_upload(
            record.job_id,
            content_length=20,
            stream=io.BytesIO(b"short"),
            keep_record_on_error=False,
        )

    assert list((tmp_path / "jobs").iterdir()) == []


def test_job_manager_rejects_when_queue_limit_is_reached(tmp_path: Path):
    manager = JobManager(
        ServiceConfig(state_dir=tmp_path, planner=PlannerConfig(), infer_script="/fake/infer.py", max_queued_jobs=1)
    )

    manager.submit_upload(
        filename="first.mp4",
        content_length=len(b"video-bytes"),
        stream=io.BytesIO(b"video-bytes"),
    )

    with pytest.raises(service.QueueFullError):
        manager.submit_upload(
            filename="second.mp4",
            content_length=len(b"video-bytes"),
            stream=io.BytesIO(b"video-bytes"),
        )


def test_cancel_queued_job_marks_it_cancelled(tmp_path: Path):
    manager = JobManager(ServiceConfig(state_dir=tmp_path, planner=PlannerConfig(), infer_script="/fake/infer.py"))
    record = manager.submit_upload(
        filename="clip.mp4",
        content_length=len(b"video-bytes"),
        stream=io.BytesIO(b"video-bytes"),
    )

    cancelled = manager.cancel_job(record.job_id)
    payload = manager.get_payload(record.job_id)

    assert cancelled.status == "cancelled"
    assert payload["status"] == "cancelled"
    assert payload["progress"]["phase"] == "cancelled"
    assert payload["cancel_requested_at"] is not None


def test_cancel_idle_upload_session_marks_it_cancelled(tmp_path: Path):
    manager = JobManager(ServiceConfig(state_dir=tmp_path, planner=PlannerConfig(), infer_script="/fake/infer.py"))
    record = manager.create_upload_session(filename="clip.mp4", content_length=100)

    cancelled = manager.cancel_job(record.job_id)
    payload = manager.get_payload(record.job_id)

    assert cancelled.status == "cancelled"
    assert payload["status"] == "cancelled"
    assert payload["cancel_requested_at"] is not None
    assert payload["progress"]["phase"] == "cancelled"
    assert payload["input"]["uploaded_bytes"] == 0


def test_job_payload_reports_percent_eta_and_current_chunk(tmp_path: Path):
    manager = JobManager(ServiceConfig(state_dir=tmp_path, planner=PlannerConfig(), infer_script="/fake/infer.py"))
    manifest_path = tmp_path / "jobs" / "job-progress" / "work" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    _write_progress_manifest(manifest_path)

    started_at = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    record = JobRecord(
        job_id="job-progress",
        status="running",
        created_at=started_at,
        updated_at=started_at,
        input_filename="clip.mp4",
        input_size_bytes=11,
        uploaded_size_bytes=11,
        input_path=str((manifest_path.parent / "input.mp4").resolve()),
        output_path=str((manifest_path.parent / "output.mp4").resolve()),
        work_dir=str(manifest_path.parent.resolve()),
        manifest_path=str(manifest_path.resolve()),
        scale=2.0,
        started_at=started_at,
    )
    manager.store.save(record)

    payload = manager.get_payload("job-progress")

    assert payload["progress"]["phase"] == "rendering"
    assert payload["progress"]["total_chunks"] == 3
    assert payload["progress"]["done_chunks"] == 1
    assert payload["progress"]["running_chunks"] == 1
    assert payload["progress"]["done_source_frames"] == 21
    assert payload["progress"]["total_source_frames"] == 50
    assert payload["progress"]["percent"] == 42.0
    assert payload["progress"]["estimated_remaining_seconds"] is not None
    assert payload["progress"]["estimated_remaining_seconds"] > 0
    assert payload["progress"]["current_chunk"]["index"] == 1
    assert payload["progress"]["current_chunk"]["source_start"] == 21
    assert payload["progress"]["upload_percent"] == 100.0


def test_job_payload_reports_upload_progress_before_processing(tmp_path: Path):
    manager = JobManager(ServiceConfig(state_dir=tmp_path, planner=PlannerConfig(), infer_script="/fake/infer.py"))
    created_at = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    record = JobRecord(
        job_id="job-uploading",
        status="uploading",
        created_at=created_at,
        updated_at=created_at,
        input_filename="clip.mp4",
        input_size_bytes=100,
        uploaded_size_bytes=40,
        input_path=str((tmp_path / "jobs" / "job-uploading" / "input" / "input.mp4").resolve()),
        output_path=str((tmp_path / "jobs" / "job-uploading" / "result" / "output.mp4").resolve()),
        work_dir=str((tmp_path / "jobs" / "job-uploading" / "work").resolve()),
        manifest_path=str((tmp_path / "jobs" / "job-uploading" / "work" / "manifest.json").resolve()),
        scale=2.0,
    )
    manager.store.save(record)

    payload = manager.get_payload("job-uploading")

    assert payload["status"] == "uploading"
    assert payload["input"]["uploaded_bytes"] == 40
    assert payload["input"]["upload_percent"] == 40.0
    assert payload["urls"]["upload"] == "/v1/jobs/job-uploading/upload"
    assert payload["progress"]["phase"] == "uploading"
    assert payload["progress"]["percent"] == 40.0
    assert payload["progress"]["upload_percent"] == 40.0
    assert payload["progress"]["uploaded_bytes"] == 40
    assert payload["progress"]["total_upload_bytes"] == 100
    assert payload["progress"]["estimated_remaining_seconds"] is not None
    assert payload["progress"]["estimated_remaining_seconds"] > 0


def test_cancel_active_upload_transitions_to_cancelled(tmp_path: Path):
    manager = JobManager(ServiceConfig(state_dir=tmp_path, planner=PlannerConfig(), infer_script="/fake/infer.py"))
    payload = b"a" * (1024 * 1024 + 256)
    stream = BlockingUploadStream(payload, first_chunk_size=1024 * 1024)
    record = manager.create_upload_session(filename="clip.mp4", content_length=len(payload))
    upload_result: dict[str, object] = {}

    def _run_upload():
        try:
            manager.upload_to_job(record.job_id, content_length=len(payload), stream=stream)
            upload_result["status"] = "completed"
        except service.UploadCancelled as exc:
            upload_result["status"] = "cancelled"
            upload_result["error"] = str(exc)

    thread = threading.Thread(target=_run_upload, daemon=True)
    thread.start()
    assert stream.first_chunk_sent.wait(timeout=5)

    cancelling = manager.cancel_job(record.job_id)
    assert cancelling.status == "cancelling"

    thread.join(timeout=5)
    assert not thread.is_alive()
    assert upload_result["status"] == "cancelled"

    final_record = manager.store.load(record.job_id)
    assert final_record.status == "cancelled"
    assert final_record.cancel_requested_at is not None
    assert final_record.uploaded_size_bytes == 1024 * 1024
    assert not Path(final_record.input_path).exists()
    assert not Path(final_record.input_path).with_name(f"{Path(final_record.input_path).name}.part").exists()


def test_running_job_can_transition_to_cancelling_then_cancelled(tmp_path: Path, monkeypatch):
    started_event = threading.Event()
    monkeypatch.setattr(service, "plan_video_job", _fake_plan_video_job)
    monkeypatch.setattr(service, "run_manifest", _make_cancellable_fake_run(started_event))

    manager = JobManager(ServiceConfig(state_dir=tmp_path, planner=PlannerConfig(), infer_script="/fake/infer.py"))
    manager.start()
    try:
        record = manager.submit_upload(
            filename="clip.mp4",
            content_length=len(b"video-bytes"),
            stream=io.BytesIO(b"video-bytes"),
        )

        assert started_event.wait(timeout=5)
        cancelling = manager.cancel_job(record.job_id)
        assert cancelling.status == "cancelling"

        payload = _wait_for_status(manager, record.job_id, "cancelled")
        assert payload["status"] == "cancelled"
        assert payload["progress"]["phase"] == "cancelled"
        assert payload["cancel_requested_at"] is not None
    finally:
        manager.stop()


def test_job_manager_processes_uploaded_video_asynchronously(tmp_path: Path, monkeypatch):
    _install_fake_runner(monkeypatch)
    manager = JobManager(ServiceConfig(state_dir=tmp_path, planner=PlannerConfig(), infer_script="/fake/infer.py"))
    manager.start()
    try:
        record = manager.submit_upload(
            filename="clip.mp4",
            content_length=len(b"video-bytes"),
            stream=io.BytesIO(b"video-bytes"),
        )

        payload = _wait_for_status(manager, record.job_id, "succeeded")

        assert payload["result"]["ready"] is True
        assert payload["result"]["size_bytes"] == len(b"fake-mp4")
        assert payload["result"]["accept_ranges"] is True
        assert payload["progress"]["done_chunks"] == payload["progress"]["total_chunks"]
        assert payload["progress"]["percent"] == 100.0
        assert Path(manager.store.load(record.job_id).output_path).read_bytes() == b"fake-mp4"
    finally:
        manager.stop()


def test_http_service_accepts_upload_status_and_result_download(tmp_path: Path, monkeypatch):
    _install_fake_runner(monkeypatch)
    manager = JobManager(ServiceConfig(state_dir=tmp_path, planner=PlannerConfig(), infer_script="/fake/infer.py"))
    manager.start()
    server = FlashVSRHTTPServer(("127.0.0.1", 0), manager)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        request = urllib.request.Request(
            f"{base_url}/v1/jobs?filename=clip.mp4",
            data=b"video-bytes",
            headers={"Content-Type": "application/octet-stream", "X-Filename": "clip.mp4"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 202
            submit_payload = json.loads(response.read().decode("utf-8"))

        job_id = submit_payload["job_id"]
        deadline = time.time() + 5
        status_payload = {}
        while time.time() < deadline:
            with urllib.request.urlopen(f"{base_url}/v1/jobs/{job_id}", timeout=5) as response:
                status_payload = json.loads(response.read().decode("utf-8"))
            if status_payload["status"] == "succeeded":
                break
            time.sleep(0.02)

        assert status_payload["status"] == "succeeded"
        with urllib.request.urlopen(f"{base_url}/v1/jobs/{job_id}/result", timeout=5) as response:
            assert response.status == 200
            assert response.headers["Content-Type"] == "video/mp4"
            assert response.headers["Accept-Ranges"] == "bytes"
            assert response.read() == b"fake-mp4"

        range_request = urllib.request.Request(
            f"{base_url}/v1/jobs/{job_id}/result",
            headers={"Range": "bytes=1-3"},
        )
        with urllib.request.urlopen(range_request, timeout=5) as response:
            assert response.status == 206
            assert response.headers["Content-Range"] == "bytes 1-3/8"
            assert response.read() == b"ake"

        delete_request = urllib.request.Request(f"{base_url}/v1/jobs/{job_id}", method="DELETE")
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(delete_request, timeout=5)
        assert excinfo.value.code == 409
    finally:
        server.shutdown()
        server.server_close()
        manager.stop()
        thread.join(timeout=1)


def test_http_service_downloads_result_with_unicode_filename(tmp_path: Path, monkeypatch):
    _install_fake_runner(monkeypatch)
    manager = JobManager(ServiceConfig(state_dir=tmp_path, planner=PlannerConfig(), infer_script="/fake/infer.py"))
    manager.start()
    server = FlashVSRHTTPServer(("127.0.0.1", 0), manager)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        record = manager.submit_upload(
            filename="李敖惊人论断.mp4",
            content_length=len(b"video-bytes"),
            stream=io.BytesIO(b"video-bytes"),
        )
        _wait_for_status(manager, record.job_id, "succeeded")

        with urllib.request.urlopen(f"{base_url}/v1/jobs/{record.job_id}/result", timeout=5) as response:
            assert response.status == 200
            assert response.read() == b"fake-mp4"
            disposition = response.headers["Content-Disposition"]

        output_name = Path(manager.store.load(record.job_id).output_path).name
        assert 'filename="' in disposition
        assert "filename*=UTF-8''" in disposition
        assert urllib.parse.quote(output_name, safe="!#$&+-.^_`|~") in disposition
    finally:
        server.shutdown()
        server.server_close()
        manager.stop()
        thread.join(timeout=1)


def test_http_service_supports_create_then_upload_with_progress(tmp_path: Path, monkeypatch):
    _install_fake_runner(monkeypatch)
    manager = JobManager(ServiceConfig(state_dir=tmp_path, planner=PlannerConfig(), infer_script="/fake/infer.py"))
    manager.start()
    server = FlashVSRHTTPServer(("127.0.0.1", 0), manager)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        create_request = urllib.request.Request(
            f"{base_url}/v1/jobs",
            data=json.dumps({"filename": "clip.mp4", "size_bytes": len(b"video-bytes")}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(create_request, timeout=5) as response:
            assert response.status == 201
            create_payload = json.loads(response.read().decode("utf-8"))

        job_id = create_payload["job_id"]
        assert create_payload["status"] == "uploading"
        assert create_payload["input"]["uploaded_bytes"] == 0
        assert create_payload["progress"]["phase"] == "uploading"
        assert create_payload["urls"]["upload"] == f"/v1/jobs/{job_id}/upload"

        with urllib.request.urlopen(f"{base_url}/v1/jobs/{job_id}", timeout=5) as response:
            status_payload = json.loads(response.read().decode("utf-8"))
        assert status_payload["status"] == "uploading"
        assert status_payload["progress"]["upload_percent"] == 0.0

        upload_request = urllib.request.Request(
            f"{base_url}/v1/jobs/{job_id}/upload",
            data=b"video-bytes",
            headers={"Content-Type": "application/octet-stream"},
            method="PUT",
        )
        with urllib.request.urlopen(upload_request, timeout=5) as response:
            assert response.status == 202
            upload_payload = json.loads(response.read().decode("utf-8"))
        assert upload_payload["input"]["uploaded_bytes"] == len(b"video-bytes")

        payload = _wait_for_status(manager, job_id, "succeeded")
        assert payload["progress"]["upload_percent"] == 100.0
        assert payload["result"]["ready"] is True
    finally:
        server.shutdown()
        server.server_close()
        manager.stop()
        thread.join(timeout=1)


def test_http_service_can_cancel_idle_upload_session(tmp_path: Path, monkeypatch):
    _install_fake_runner(monkeypatch)
    manager = JobManager(ServiceConfig(state_dir=tmp_path, planner=PlannerConfig(), infer_script="/fake/infer.py"))
    manager.start()
    server = FlashVSRHTTPServer(("127.0.0.1", 0), manager)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        create_request = urllib.request.Request(
            f"{base_url}/v1/jobs",
            data=json.dumps({"filename": "clip.mp4", "size_bytes": len(b"video-bytes")}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(create_request, timeout=5) as response:
            create_payload = json.loads(response.read().decode("utf-8"))

        job_id = create_payload["job_id"]
        delete_request = urllib.request.Request(f"{base_url}/v1/jobs/{job_id}", method="DELETE")
        with urllib.request.urlopen(delete_request, timeout=5) as response:
            assert response.status == 200
            cancel_payload = json.loads(response.read().decode("utf-8"))

        assert cancel_payload["status"] == "cancelled"
        assert cancel_payload["progress"]["phase"] == "cancelled"
    finally:
        server.shutdown()
        server.server_close()
        manager.stop()
        thread.join(timeout=1)
