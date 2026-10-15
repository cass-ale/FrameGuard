"""Bug-sweep regression tests (deep-scan stability, banner lifecycle, severity).

Covers the fixes from the FGv0.2.2 hardening pass. All run headless via the
offscreen Qt platform plugin. No test touches the network or real media files:
ffmpeg interaction is mocked at the subprocess boundary.
"""

import io
import subprocess
import threading
from unittest import mock

import pytest

from frameguard.core import (
    CHECK_SEVERITY_WEIGHTS,
    DEFAULT_CONFIG,
    QAResult,
    _cap_stderr_text,
    _run_segment_filter_scan,
    check_severity,
    discover_video_files,
    result_severity,
    run_deep_scan,
    run_ffprobe,
)

pytestmark = pytest.mark.usefixtures("qapp")


def _cfg(**overrides):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(overrides)
    return cfg


def _finalized(name, outcome="fail", **kw):
    from conftest import make_result
    return make_result(name, outcome, **kw)


class _GeneratedBinaryStream:
    """Lazy prefix + repeated bytes + suffix stream with no newline."""

    def __init__(self, prefix=b"", noise_bytes=0, suffix=b""):
        self.prefix = prefix
        self.noise_bytes = noise_bytes
        self.suffix = suffix
        self.max_read_size = 0
        self.closed = False

    def __iter__(self):
        raise AssertionError("capture must use bounded binary reads, not line iteration")

    def read(self, size=-1):
        assert size > 0, "capture must request a bounded chunk"
        self.max_read_size = max(self.max_read_size, size)
        if self.closed:
            return b""
        out = bytearray()
        if self.prefix and len(out) < size:
            take = min(size - len(out), len(self.prefix))
            out += self.prefix[:take]
            self.prefix = self.prefix[take:]
        if self.noise_bytes and len(out) < size:
            take = min(size - len(out), self.noise_bytes)
            out += b"x" * take
            self.noise_bytes -= take
        if not self.noise_bytes and self.suffix and len(out) < size:
            take = min(size - len(out), len(self.suffix))
            out += self.suffix[:take]
            self.suffix = self.suffix[take:]
        return bytes(out)

    def close(self):
        self.closed = True


class _BinaryProc:
    _next_pid = 10000

    def __init__(self, stderr, stdout=b"out_time_us=1000000\nprogress=end\n",
                 returncode=0):
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.stderr = stderr
        self.stdout = io.BytesIO(stdout)
        self.returncode = returncode
        self.killed = 0

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed += 1
        self.returncode = -9
        self.stderr.close()


# ---------------------------------------------------------------------------
# Phase A: deep-scan stability
# ---------------------------------------------------------------------------

class TestSingleTerminalEmit:
    def test_cancel_emits_only_scan_cancelled(self, tmp_path):
        """run() used to emit BOTH scan_finished and scan_cancelled on cancel,
        double-running GUI cleanup, auto-export and the watch-queue drain."""
        from frameguard.worker import ScanWorker

        w = ScanWorker()
        w.configure(files=[str(tmp_path / "a.mov")])
        w._cancel_event.set()  # cancel before the first file

        terminals = []
        w.scan_finished.connect(lambda r: terminals.append("finished"))
        w.scan_cancelled.connect(lambda: terminals.append("cancelled"))
        w.scan_error.connect(lambda m: terminals.append("error"))
        # Hermetic: the tool preflight must pass so we reach the cancel path
        # on machines with and without ffmpeg installed.
        with mock.patch("frameguard.tools.get_ffmpeg_path",
                        return_value="ffmpeg"), \
             mock.patch("frameguard.tools.get_ffprobe_path",
                        return_value="ffprobe"):
            w.run()

        assert terminals == ["cancelled"], terminals

    def test_no_files_is_an_error_not_a_cancel(self):
        from frameguard.worker import ScanWorker

        w = ScanWorker()
        w.configure(files=[])
        terminals = []
        w.scan_finished.connect(lambda r: terminals.append("finished"))
        w.scan_cancelled.connect(lambda: terminals.append("cancelled"))
        w.scan_error.connect(lambda m: terminals.append("error"))
        w.run()
        assert terminals == ["error"]

    def test_worker_passes_itself_as_the_scan_process_owner(self, tmp_path):
        from frameguard.worker import ScanWorker

        worker = ScanWorker()
        worker.configure(files=[str(tmp_path / "owned.mov")])
        result = QAResult("owned.mov")
        with mock.patch("frameguard.tools.get_ffmpeg_path", return_value="ffmpeg"), \
             mock.patch("frameguard.tools.get_ffprobe_path", return_value="ffprobe"), \
             mock.patch("frameguard.worker.run_qa_checks", return_value=result) as run:
            worker.run()

        assert run.call_args.kwargs["process_owner"] is worker

    def test_missing_tools_is_a_clear_error(self, tmp_path):
        """Popen(None) used to raise TypeError mid-scan, surfacing as a cryptic
        per-file 'Internal Error' instead of a clear missing-tools message."""
        from frameguard.worker import ScanWorker

        w = ScanWorker()
        w.configure(files=[str(tmp_path / "a.mov")])
        errors = []
        w.scan_error.connect(errors.append)
        with mock.patch("frameguard.tools.get_ffmpeg_path", return_value=None), \
             mock.patch("frameguard.tools.get_ffprobe_path", return_value=None):
            w.run()
        assert errors and "not found" in errors[0].lower()


class TestCancelWithoutPsutil:
    def test_cancel_kills_tracked_procs_when_psutil_missing(self):
        """Without psutil, cancel() was a no-op and a deep scan ran for up to
        two hours before noticing the cancel."""
        import builtins
        from frameguard.worker import ScanWorker

        w = ScanWorker()
        real_import = builtins.__import__

        killed = []

        class _FakeProc:
            def __init__(self):
                self.alive = True

            def terminate(self):
                killed.append("terminate")
                self.alive = False

            def poll(self):
                return None if self.alive else 0

            def wait(self, timeout=None):
                self.alive = False
                return 0

            def kill(self):
                killed.append("kill")
                self.alive = False

        w.register_proc(_FakeProc())

        def _no_psutil(name, *args, **kwargs):
            if name == "psutil":
                raise ImportError("No module named 'psutil'")
            return real_import(name, *args, **kwargs)

        with mock.patch.object(builtins, "__import__", _no_psutil):
            w.cancel()

        assert "terminate" in killed

    def test_cancel_does_not_reach_another_workers_core_handle(self):
        """The no-psutil fallback must never append every globally live proc."""
        import builtins
        from frameguard import core as C
        from frameguard.worker import ScanWorker

        owner = ScanWorker()
        other = ScanWorker()
        real_import = builtins.__import__

        class _FakeProc:
            def __init__(self, pid):
                self.pid = pid
                self.terminated = 0

            def terminate(self):
                self.terminated += 1

            def poll(self):
                return None

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        owned_proc = _FakeProc(1001)
        other_proc = _FakeProc(2002)
        owner.register_proc(owned_proc)
        other.register_proc(other_proc)
        C._track_proc(other_proc)
        try:
            def _no_psutil(name, *args, **kwargs):
                if name == "psutil":
                    raise ImportError("No module named 'psutil'")
                return real_import(name, *args, **kwargs)

            with mock.patch.object(builtins, "__import__", _no_psutil):
                owner.cancel()
        finally:
            C._untrack_proc(other_proc)

        assert owned_proc.terminated == 1
        assert other_proc.terminated == 0

    def test_exited_owned_process_is_not_terminated_as_a_reused_pid(self):
        import builtins
        from frameguard.worker import ScanWorker

        worker = ScanWorker()
        real_import = builtins.__import__

        class _ExitedProc:
            pid = 31337
            terminated = 0

            def poll(self):
                return 0

            def terminate(self):
                self.terminated += 1

        proc = _ExitedProc()
        worker.register_proc(proc)

        def _no_psutil(name, *args, **kwargs):
            if name == "psutil":
                raise ImportError("No module named 'psutil'")
            return real_import(name, *args, **kwargs)

        with mock.patch.object(builtins, "__import__", _no_psutil):
            worker.cancel()

        assert proc.terminated == 0

    def test_registration_after_cancel_terminates_the_late_process(self):
        import builtins
        from frameguard.worker import ScanWorker

        worker = ScanWorker()
        real_import = builtins.__import__

        class _LateProc:
            pid = 41414
            terminated = 0

            def poll(self):
                return None

            def terminate(self):
                self.terminated += 1

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        def _no_psutil(name, *args, **kwargs):
            if name == "psutil":
                raise ImportError("No module named 'psutil'")
            return real_import(name, *args, **kwargs)

        with mock.patch.object(builtins, "__import__", _no_psutil):
            worker.cancel()
        proc = _LateProc()
        worker.register_proc(proc)
        assert proc.terminated == 1

        with mock.patch.object(builtins, "__import__", _no_psutil):
            worker.cancel()
        assert proc.terminated == 1


class TestPerWorkerPsutilOwnership:
    def test_cancel_terminates_only_the_calling_workers_pid(self, monkeypatch):
        import types
        from frameguard.worker import ScanWorker

        owner = ScanWorker()
        other = ScanWorker()

        class _Popen:
            def __init__(self, pid):
                self.pid = pid
                self.alive = True

            def poll(self):
                return None if self.alive else 0

        owned_proc = _Popen(501)
        other_proc = _Popen(502)
        owner.register_proc(owned_proc)
        other.register_proc(other_proc)
        terminated = []

        class _PsutilProc:
            def __init__(self, pid):
                self.pid = pid

            def terminate(self):
                terminated.append(self.pid)

            def wait(self, timeout=None):
                return 0

            def kill(self):
                terminated.append(-self.pid)

        fake_psutil = types.SimpleNamespace(
            Process=_PsutilProc,
            NoSuchProcess=type("NoSuchProcess", (Exception,), {}),
            AccessDenied=type("AccessDenied", (Exception,), {}),
            TimeoutExpired=type("TimeoutExpired", (Exception,), {}),
        )
        monkeypatch.setitem(__import__("sys").modules, "psutil", fake_psutil)

        owner.cancel()
        assert terminated == [owned_proc.pid]

    def test_create_time_mismatch_protects_a_reused_pid(self, monkeypatch):
        import types
        from frameguard.worker import ScanWorker

        create_time = {601: 1.0}
        terminated = []

        class _PsutilProc:
            def __init__(self, pid):
                self.pid = pid

            def create_time(self):
                return create_time[self.pid]

            def terminate(self):
                terminated.append(self.pid)

            def wait(self, timeout=None):
                return 0

            def kill(self):
                terminated.append(-self.pid)

        fake_psutil = types.SimpleNamespace(
            Process=_PsutilProc,
            NoSuchProcess=type("NoSuchProcess", (Exception,), {}),
            AccessDenied=type("AccessDenied", (Exception,), {}),
            TimeoutExpired=type("TimeoutExpired", (Exception,), {}),
        )
        monkeypatch.setitem(__import__("sys").modules, "psutil", fake_psutil)

        class _Popen:
            pid = 601

            def poll(self):
                return None

        worker = ScanWorker()
        worker.register_proc(_Popen())
        create_time[601] = 2.0
        worker.cancel()
        assert terminated == []


class TestBlockingProbeCancellation:
    def test_cancel_interrupts_owned_ffprobe_without_touching_other_workers(self):
        import builtins
        from frameguard.worker import ScanWorker

        owner = ScanWorker()
        other = ScanWorker()
        started = threading.Event()
        stopped = threading.Event()
        real_import = builtins.__import__

        class _BlockingProc:
            def __init__(self, pid):
                self.pid = pid
                self.returncode = None
                self.terminated = 0

            def communicate(self, timeout=None):
                started.set()
                assert stopped.wait(2), "owned ffprobe was not cancelled"
                self.returncode = -15
                return "", ""

            def poll(self):
                return None if not stopped.is_set() else self.returncode

            def terminate(self):
                self.terminated += 1
                stopped.set()

            def kill(self):
                stopped.set()

        owned_proc = _BlockingProc(701)
        other_proc = _BlockingProc(702)
        other.register_proc(other_proc)
        result = []

        with mock.patch("frameguard.core.get_ffprobe_path", return_value="ffprobe"), \
             mock.patch("frameguard.core.subprocess.Popen", return_value=owned_proc):
            thread = threading.Thread(
                target=lambda: result.append(run_ffprobe(
                    "clip.mp4", owner, owner._cancel_event)))
            thread.start()
            assert started.wait(1)

            def _no_psutil(name, *args, **kwargs):
                if name == "psutil":
                    raise ImportError("No module named 'psutil'")
                return real_import(name, *args, **kwargs)

            with mock.patch.object(builtins, "__import__", _no_psutil):
                owner.cancel()
            thread.join(timeout=1)

        assert not thread.is_alive()
        assert result == [None]
        assert owned_proc.terminated == 1
        assert other_proc.terminated == 0


class TestStderrCap:
    def test_huge_stderr_is_bounded(self):
        text = "\n".join(f"[info] frame={i} fps=30" for i in range(100_000))
        cfg = _cfg(deep_scan_max_stderr_lines=5000,
                   deep_scan_max_stderr_chars=512 * 1024)
        capped, truncated = _cap_stderr_text(text, cfg)
        assert truncated > 0
        assert len(capped.split("\n")) <= 5000
        assert len(capped) <= 512 * 1024

    def test_small_stderr_passes_through_untouched(self):
        capped, truncated = _cap_stderr_text("a\nb\nc", _cfg())
        assert (capped, truncated) == ("a\nb\nc", 0)

    def test_early_evidence_survives_line_and_character_caps(self):
        evidence = (
            "[h264] Invalid NAL unit size (-1 > 100).\n"
            "black_start:5 black_end:15 black_duration:10\n"
            "silence_start: 6\nsilence_end: 14 | silence_duration: 8"
        )
        noise = "\n".join(f"frame={i} " + "x" * 80 for i in range(200))
        capped, truncated = _cap_stderr_text(
            evidence + "\n" + noise,
            _cfg(deep_scan_max_stderr_lines=8, deep_scan_max_stderr_chars=600),
        )

        assert truncated > 0
        assert len(capped.splitlines()) <= 8
        assert len(capped) <= 600
        assert "Invalid NAL unit size" in capped
        assert "black_start:5" in capped and "black_end:15" in capped
        assert "silence_start: 6" in capped and "silence_end: 14" in capped

    def test_evidence_allocation_keeps_first_recent_and_deduplicates(self):
        lines = [
            "black_start:0 black_end:1 black_duration:1",
            *(f"black_start:{i} black_end:{i + 1} black_duration:1"
              for i in range(1, 40)),
            "freeze_start:20 freeze_end:25 freeze_duration:5",
            "[h264] Invalid NAL unit size (-1 > 100).",
            "[h264] Invalid NAL unit size (-1 > 100).",
            *(f"black_start:{100 + i} black_end:{101 + i} black_duration:1"
              for i in range(40)),
            "frame=500",
        ]
        capped, truncated = _cap_stderr_text(
            "\n".join(lines),
            _cfg(deep_scan_max_stderr_lines=10,
                 deep_scan_max_stderr_chars=1200),
        )

        assert truncated > 0
        assert capped.count("Invalid NAL unit size") == 1
        assert "freeze_start:20" in capped
        assert "black_start:0" in capped
        assert "black_start:139" in capped
        assert capped.count("black_start:") == 2
        assert "frame=500" in capped

    def test_segment_multimegabyte_unterminated_record_uses_bounded_binary_reads(self):
        stream = _GeneratedBinaryStream(
            prefix=(b"\xffblack_start:5 black_end:15 black_duration:10 "),
            noise_bytes=3 * 1024 * 1024,
            suffix=(b" freeze_start:20 freeze_end:25 freeze_duration:5 "
                    b"Invalid NAL unit size (-1 > 100). frame=300"),
        )
        proc = _BinaryProc(stream)
        cfg = _cfg(deep_scan_max_stderr_lines=10,
                   deep_scan_max_stderr_chars=1200)
        with mock.patch("frameguard.core.get_ffmpeg_path", return_value="ffmpeg"), \
             mock.patch("frameguard.core._get_optimal_hwaccel", return_value=[]), \
             mock.patch("frameguard.core.subprocess.Popen", return_value=proc):
            captured = _run_segment_filter_scan(
                "clip.mp4", 0, 30, "blackdetect", None, False, cfg)

        assert captured is not None
        assert stream.max_read_size <= 64 * 1024
        assert captured.peak_pending_chars <= 70 * 1024
        assert len(captured) <= 1200
        assert "\ufffdblack_start:5" in captured
        assert "freeze_start:20" in captured
        assert "Invalid NAL unit size" in captured

    def test_late_distinct_evidence_survives_category_pressure_end_to_end(self):
        def noisy_stream():
            early_black = b"".join(
                f"black_start:{i} black_end:{i + 1} black_duration:1\n".encode()
                for i in range(40)
            )
            late_distinct = (
                b"freeze_start:20 freeze_end:25 freeze_duration:5\n"
                b"[h264] Invalid NAL unit size (-1 > 100).\n"
            )
            later_black = b"".join(
                f"black_start:{100 + i} black_end:{101 + i} black_duration:1\n".encode()
                for i in range(40)
            )
            return io.BytesIO(early_black + late_distinct + later_black + b"frame=500\n")

        procs = []

        def make_proc(*args, **kwargs):
            proc = _BinaryProc(noisy_stream())
            procs.append(proc)
            return proc

        result = QAResult("clip.mp4")
        cfg = _cfg(
            segment_enabled=True, segment_count=2, segment_min_duration=1,
            deep_scan_max_stderr_lines=10, deep_scan_max_stderr_chars=1200,
        )
        with mock.patch("frameguard.core.get_file_duration", return_value=40.0), \
             mock.patch("frameguard.core.os.path.getsize", return_value=1024), \
             mock.patch("frameguard.core.get_ffmpeg_path", return_value="ffmpeg"), \
             mock.patch("frameguard.core._get_optimal_hwaccel", return_value=[]), \
             mock.patch("frameguard.core.subprocess.Popen", side_effect=make_proc):
            run_deep_scan(
                "clip.mp4", result, cfg,
                probe_data={"streams": [{"codec_type": "video"}]},
            )

        result.finalize()
        decode = next(c for c in result.checks if c[0] == "Decode Integrity")
        freeze = next(c for c in result.checks if c[0] == "Freeze Frames")
        assert len(procs) == 2
        assert decode[1] == "FAIL", result.checks
        assert freeze[1] in ("WARN", "FAIL"), result.checks

    def test_single_pass_multimegabyte_unterminated_record_changes_verdict(self):
        stream = _GeneratedBinaryStream(
            prefix=b"header \xff ",
            noise_bytes=3 * 1024 * 1024,
            suffix=(b" freeze_start:20 freeze_end:25 freeze_duration:5 "
                    b"Invalid NAL unit size (-1 > 100). frame=300"),
        )
        proc = _BinaryProc(stream)
        result = QAResult("clip.mp4")
        cfg = _cfg(
            segment_enabled=False,
            deep_scan_max_stderr_lines=10,
            deep_scan_max_stderr_chars=1200,
        )
        with mock.patch("frameguard.core.get_file_duration", return_value=40.0), \
             mock.patch("frameguard.core.os.path.getsize", return_value=1024), \
             mock.patch("frameguard.core.get_ffmpeg_path", return_value="ffmpeg"), \
             mock.patch("frameguard.core._get_optimal_hwaccel", return_value=[]), \
             mock.patch("frameguard.core.subprocess.Popen", return_value=proc):
            run_deep_scan(
                "clip.mp4", result, cfg,
                probe_data={"streams": [{"codec_type": "video"}]},
            )

        result.finalize()
        decode = next(c for c in result.checks if c[0] == "Decode Integrity")
        freeze = next(c for c in result.checks if c[0] == "Freeze Frames")
        assert stream.max_read_size <= 64 * 1024
        assert decode[1] == "FAIL", result.checks
        assert freeze[1] in ("WARN", "FAIL"), result.checks

    def test_segment_capture_is_bounded_while_process_is_running(self):
        class _NoisyStderr:
            def __init__(self, line_factory, drained):
                self._lines = iter(line_factory())
                self._pending = b""
                self._drained = drained

            def read(self, size=-1):
                assert size > 0
                while len(self._pending) < size:
                    try:
                        self._pending += next(self._lines).encode()
                    except StopIteration:
                        break
                chunk, self._pending = self._pending[:size], self._pending[size:]
                if not chunk:
                    self._drained.set()
                return chunk

            def close(self):
                self._pending = b""
                self._drained.set()

        class _NoisyProc:
            pid = 8181
            returncode = 0

            def __init__(self):
                self.communicate_called = False
                self.drained = threading.Event()
                self.stderr = _NoisyStderr(self._lines, self.drained)

            def _lines(self):
                yield "black_start:5 black_end:15 black_duration:10\n"
                for i in range(20_000):
                    yield f"frame={i} " + "x" * 100 + "\n"
                yield "[h264] Invalid NAL unit size (-1 > 100).\n"
                yield "frame= 20000 fps=30\n"

            def communicate(self, timeout=None):
                self.communicate_called = True
                return "", "".join(self._lines())

            def wait(self, timeout=None):
                assert self.drained.wait(2), "stderr reader did not drain the pipe"
                return self.returncode

            def kill(self):
                self.returncode = -9

        proc = _NoisyProc()
        cfg = _cfg(deep_scan_max_stderr_lines=8,
                   deep_scan_max_stderr_chars=600)
        with mock.patch("frameguard.core.get_ffmpeg_path", return_value="ffmpeg"), \
             mock.patch("frameguard.core._get_optimal_hwaccel", return_value=[]), \
             mock.patch("frameguard.core.subprocess.Popen", return_value=proc):
            captured = _run_segment_filter_scan(
                "clip.mp4", 0, 10, "blackdetect", None, False, cfg)

        assert not proc.communicate_called
        assert len(captured.splitlines()) <= 8
        assert len(captured) <= 600
        assert getattr(captured, "truncated_records", 0) > 0
        assert captured.peak_lines <= 8
        assert captured.peak_chars <= 600
        assert "black_start:5" in captured
        assert "Invalid NAL unit size" in captured
        assert "frame= 20000" in captured

    def test_segment_capture_evidence_reaches_end_to_end_verdict(self):
        class _NoisyProc:
            _next_pid = 9000

            def __init__(self):
                type(self)._next_pid += 1
                self.pid = type(self)._next_pid
                self.returncode = 0
                self.stderr = io.BytesIO(b"".join([
                    b"black_start:5 black_end:15 black_duration:10\n",
                    *(f"frame={i} ".encode() + b"x" * 100 + b"\n"
                      for i in range(500)),
                    b"[h264] Invalid NAL unit size (-1 > 100).\n",
                    b"frame= 500 fps=30\n",
                ]))

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.returncode = -9

        result = QAResult("clip.mp4")
        procs = []

        def make_proc(*args, **kwargs):
            proc = _NoisyProc()
            procs.append(proc)
            return proc

        cfg = _cfg(
            segment_enabled=True, segment_count=2, segment_min_duration=1,
            deep_scan_max_stderr_lines=6, deep_scan_max_stderr_chars=500,
        )
        with mock.patch("frameguard.core.get_file_duration", return_value=40.0), \
             mock.patch("frameguard.core.os.path.getsize", return_value=1024), \
             mock.patch("frameguard.core.get_ffmpeg_path", return_value="ffmpeg"), \
             mock.patch("frameguard.core._get_optimal_hwaccel", return_value=[]), \
             mock.patch("frameguard.core.subprocess.Popen", side_effect=make_proc):
            run_deep_scan(
                "clip.mp4", result, cfg,
                probe_data={"streams": [{"codec_type": "video"}]},
            )

        result.finalize()
        decode = next(c for c in result.checks if c[0] == "Decode Integrity")
        black = next(c for c in result.checks if c[0] == "Black Frames")
        assert len(procs) == 2
        assert decode[1] == "FAIL", result.checks
        assert black[1] in ("WARN", "FAIL"), result.checks
        assert result.metadata["deep_scan_stderr_truncated_lines"] > 0

    def test_segmented_scan_verdict_keeps_early_defect_evidence(self):
        result = QAResult("clip.mp4")
        early = (
            "[h264] Invalid NAL unit size (-1 > 100).\n"
            "black_start:5 black_end:15 black_duration:10\n"
        )
        noise = "\n".join(f"frame={i} " + "x" * 100 for i in range(100))
        cfg = _cfg(
            segment_enabled=True, segment_count=2, segment_min_duration=1,
            deep_scan_max_stderr_lines=6, deep_scan_max_stderr_chars=500,
        )
        with mock.patch("frameguard.core.get_file_duration", return_value=40.0), \
             mock.patch("frameguard.core.os.path.getsize", return_value=1024), \
             mock.patch("frameguard.core._get_optimal_hwaccel", return_value=[]), \
             mock.patch("frameguard.core._run_segment_filter_scan",
                        return_value=early + noise):
            run_deep_scan(
                "clip.mp4", result, cfg,
                probe_data={"streams": [{"codec_type": "video"}]},
            )

        result.finalize()
        decode = next(c for c in result.checks if c[0] == "Decode Integrity")
        black = next(c for c in result.checks if c[0] == "Black Frames")
        assert decode[1] == "FAIL", result.checks
        assert black[1] in ("WARN", "FAIL"), result.checks
        assert result.metadata["deep_scan_stderr_truncated_lines"] > 0

    def test_segment_scan_returns_none_immediately_when_cancelled(self):
        """A pre-set cancel_event must not spawn ffmpeg at all."""
        cfg = _cfg()
        ev = threading.Event()
        ev.set()
        with mock.patch("frameguard.core.subprocess.Popen") as popen:
            out = _run_segment_filter_scan("x.mp4", 0, 10, "blackdetect", None,
                                           False, cfg, cancel_event=ev)
        assert out is None
        popen.assert_not_called()

    def test_segment_scan_never_calls_popen_with_none_tool(self):
        """get_ffmpeg_path() -> None must short-circuit, not Popen(None)."""
        with mock.patch("frameguard.core.get_ffmpeg_path", return_value=None):
            with mock.patch("frameguard.core.subprocess.Popen") as popen:
                out = _run_segment_filter_scan("x.mp4", 0, 10, "blackdetect",
                                               None, False, _cfg())
        assert out is None
        popen.assert_not_called()

    def test_segment_timeout_kills_process_and_unblocks_stderr_reader(self):
        stopped = threading.Event()
        reader_started = threading.Event()

        class _BlockingStderr:
            def read(self, size=-1):
                assert size > 0
                reader_started.set()
                stopped.wait(2)
                return b""

            def close(self):
                stopped.set()

        class _TimedOutProc:
            pid = 9191
            returncode = None
            stderr = _BlockingStderr()

            def __init__(self):
                self.killed = 0

            def wait(self, timeout=None):
                if not stopped.is_set():
                    raise subprocess.TimeoutExpired("ffmpeg", timeout)
                self.returncode = -9
                return self.returncode

            def kill(self):
                self.killed += 1
                stopped.set()

        proc = _TimedOutProc()
        with mock.patch("frameguard.core.get_ffmpeg_path", return_value="ffmpeg"), \
             mock.patch("frameguard.core._get_optimal_hwaccel", return_value=[]), \
             mock.patch("frameguard.core.subprocess.Popen", return_value=proc) as popen:
            out = _run_segment_filter_scan(
                "x.mp4", 0, 10, "blackdetect", None, False, _cfg())

        assert reader_started.is_set()
        assert out is None
        assert proc.killed == 1
        assert popen.call_args.kwargs["stdout"] is subprocess.DEVNULL

    def test_segment_cancel_terminates_owner_and_joins_stderr_reader(self):
        import builtins
        from frameguard.worker import ScanWorker

        stopped = threading.Event()
        reader_started = threading.Event()
        real_import = builtins.__import__

        class _BlockingStderr:
            def read(self, size=-1):
                assert size > 0
                reader_started.set()
                stopped.wait(2)
                return b""

            def close(self):
                stopped.set()

        class _CancellableProc:
            pid = 9292
            returncode = None
            stderr = _BlockingStderr()

            def __init__(self):
                self.terminated = 0

            def wait(self, timeout=None):
                assert stopped.wait(2), "cancel did not stop segment process"
                self.returncode = -15
                return self.returncode

            def poll(self):
                return None if not stopped.is_set() else self.returncode

            def terminate(self):
                self.terminated += 1
                stopped.set()

            def kill(self):
                stopped.set()

        owner = ScanWorker()
        proc = _CancellableProc()
        output = []
        with mock.patch("frameguard.core.get_ffmpeg_path", return_value="ffmpeg"), \
             mock.patch("frameguard.core._get_optimal_hwaccel", return_value=[]), \
             mock.patch("frameguard.core.subprocess.Popen", return_value=proc):
            thread = threading.Thread(target=lambda: output.append(
                _run_segment_filter_scan(
                    "x.mp4", 0, 10, "blackdetect", None, False, _cfg(),
                    cancel_event=owner._cancel_event, process_owner=owner)))
            thread.start()
            assert reader_started.wait(1)

            def _no_psutil(name, *args, **kwargs):
                if name == "psutil":
                    raise ImportError("No module named 'psutil'")
                return real_import(name, *args, **kwargs)

            with mock.patch.object(builtins, "__import__", _no_psutil):
                owner.cancel()
            thread.join(timeout=1)

        assert not thread.is_alive()
        assert output == [None]
        assert proc.terminated == 1

    def test_segment_process_is_registered_to_its_explicit_owner(self):
        from frameguard.worker import ScanWorker

        worker = ScanWorker()

        class _Proc:
            pid = 9090
            returncode = 0
            stderr = io.BytesIO(b"frame= 10\n")

            def wait(self, timeout=None):
                return self.returncode

        proc = _Proc()
        with mock.patch("frameguard.core.get_ffmpeg_path", return_value="ffmpeg"), \
             mock.patch("frameguard.core._get_optimal_hwaccel", return_value=[]), \
             mock.patch("frameguard.core.subprocess.Popen", return_value=proc), \
             mock.patch.object(worker, "register_proc", wraps=worker.register_proc) as register, \
             mock.patch.object(worker, "unregister_proc", wraps=worker.unregister_proc) as unregister:
            out = _run_segment_filter_scan(
                "x.mp4", 0, 10, "blackdetect", None, False, _cfg(),
                process_owner=worker,
            )

        assert out == "frame= 10"
        register.assert_called_once_with(proc)
        unregister.assert_called_once_with(proc)


class TestSafeDiscovery:
    def test_permission_error_returns_empty_instead_of_raising(self, tmp_path):
        with mock.patch("frameguard.core.os.listdir",
                        side_effect=PermissionError("denied")):
            assert discover_video_files(str(tmp_path)) == []

    def test_missing_path_returns_empty(self, tmp_path):
        assert discover_video_files(str(tmp_path / "nope")) == []

    def test_single_file_passthrough(self, tmp_path):
        f = tmp_path / "clip.mp4"
        f.write_bytes(b"\x00")
        assert discover_video_files(str(f)) == [str(f)]

    def test_unsupported_extension_filtered(self, tmp_path):
        f = tmp_path / "notes.txt"
        f.write_text("hi")
        assert discover_video_files(str(f)) == []


class TestToolPathGuards:
    def test_run_ffprobe_with_no_tool_returns_none(self):
        with mock.patch("frameguard.core.get_ffprobe_path", return_value=None):
            with mock.patch("frameguard.core.subprocess.run") as run:
                assert run_ffprobe("x.mp4") is None
            run.assert_not_called()


class TestDeepScanDefaults:
    def test_segment_parallelism_is_capped(self):
        """8 concurrent 4K ffmpeg decodes OOM'd Windows; cap is 4."""
        assert DEFAULT_CONFIG["segment_count"] <= 4
        assert DEFAULT_CONFIG["max_workers"] <= 4

    def test_stderr_bounds_exist(self):
        assert DEFAULT_CONFIG["deep_scan_max_stderr_lines"] > 0
        assert DEFAULT_CONFIG["deep_scan_max_stderr_chars"] > 0

    def test_size_gate_exists(self):
        assert DEFAULT_CONFIG["deep_scan_max_file_gb"] > 0
        assert DEFAULT_CONFIG["deep_scan_max_duration_s"] > 0


# ---------------------------------------------------------------------------
# Phase B: banner lifecycle
# ---------------------------------------------------------------------------

class TestBannerLifecycle:
    def _panel(self):
        from frameguard.gui import ScanPanel
        return ScanPanel()

    def test_discovery_runner_delivers_without_blocking(self, tmp_path):
        """Folder discovery must reach _set_files via the background runner
        (sync os.listdir on a network mount froze the UI). The panel must
        also hold the runner: QThreadPool auto-deletes the runnable, and
        without a Python-side holder the finished signal can be lost."""
        from PySide6.QtCore import QThreadPool
        from PySide6.QtWidgets import QApplication
        from frameguard.gui import ScanPanel

        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"\x00" * 32)

        p = ScanPanel()
        p._discover_files_async([str(tmp_path)])
        assert p._discovery_runners, "runner was not retained"
        assert QThreadPool.globalInstance().waitForDone(30000)
        QApplication.processEvents()
        assert [f for f in p._files if f.endswith("clip.mp4")], p._files

    def test_new_files_clear_the_verdict(self):
        p = self._panel()
        p.show_completion(3, 1, 0)
        assert p.completion_banner.isVisibleTo(p)
        p._set_files(["a.mov"])
        assert p._banner_state == "idle"
        assert not p.completion_banner.isVisibleTo(p)

    def test_error_replaces_stale_verdict(self):
        p = self._panel()
        p.show_completion(3, 0, 0)  # clean pass banner
        assert "passed" in p.completion_banner.text().lower()
        p.show_error("boom")
        assert p._banner_state == "error"
        assert "boom" in p.completion_banner.text()
        assert p.completion_banner.isVisibleTo(p)

    def test_scan_error_path_sets_error_banner(self):
        """MainWindow._on_scan_error must not leave the prior verdict up."""
        from frameguard.gui import MainWindow
        w = MainWindow()
        w.scan_panel.show_completion(2, 0, 0)
        # QMessageBox.critical() would exec a modal dialog and block forever
        # on the offscreen platform.
        with mock.patch("frameguard.gui.QMessageBox"):
            w._on_scan_error("disk vanished")
        assert w.scan_panel._banner_state == "error"
        assert "disk vanished" in w.scan_panel.completion_banner.text()

    def test_empty_completion_hides(self):
        p = self._panel()
        p.show_completion(0, 0, 0)
        assert not p.completion_banner.isVisibleTo(p)
        assert p._banner_state == "idle"

    def test_theme_change_preserves_verdict(self):
        """apply_theme used to wipe the verdict banner."""
        from frameguard.theme import get_theme
        p = self._panel()
        p.show_completion(2, 1, 0)
        text = p.completion_banner.text()
        p.apply_theme(get_theme())
        assert p.completion_banner.isVisibleTo(p)
        assert p.completion_banner.text() == text

    def test_cancelled_state(self):
        p = self._panel()
        p.show_completion(0, 0, 0, cancelled=True)
        assert p._banner_state == "cancelled"
        assert "cancelled" in p.completion_banner.text().lower()


# ---------------------------------------------------------------------------
# Phase C: severity-weighted hierarchy
# ---------------------------------------------------------------------------

class TestSeverityWeights:
    def test_corruption_outranks_framerate(self):
        corrupt = QAResult("bad.mov")
        corrupt.add_fail("FFprobe", "could not read file")
        corrupt.finalize()

        fr = QAResult("offspec.mov")
        fr.add_pass("Container Format", "ok")
        fr.add_fail("Frame Rate", "24 != 30")
        fr.finalize()

        assert corrupt.severity_score > fr.severity_score

    def test_decode_integrity_fail_outranks_framerate_warn(self):
        """Decode Integrity FAIL used to share a bucket with Frame Rate WARN."""
        decode = QAResult("d.mov")
        decode.add_pass("Container Format", "ok")
        decode.add_fail("Decode Integrity", "3 decode errors", confidence=90)
        decode.finalize()

        fr = QAResult("f.mov")
        fr.add_pass("Container Format", "ok")
        fr.add_warn("Frame Rate", "slightly high")
        fr.finalize()

        assert decode.severity_score > fr.severity_score

    def test_clean_file_scores_zero(self):
        r = QAResult("c.mov")
        r.add_pass("Container Format", "ok")
        r.finalize()
        assert r.severity_score == 0.0

    def test_weights_cover_all_essential_checks(self):
        from frameguard.core import ESSENTIAL_CHECKS
        for name in ESSENTIAL_CHECKS:
            assert name in CHECK_SEVERITY_WEIGHTS, f"{name} has no severity weight"

    def test_sort_key_orders_corrupted_first(self):
        from frameguard.gui import ResultsPanel
        rp = ResultsPanel()

        fr = _finalized("framerate_only.mov", "fail")  # Bitrate FAIL ~60
        corrupt = QAResult("corrupt.mov")
        corrupt.add_fail("FFprobe", "unreadable")
        corrupt.finalize()

        rp.set_results([fr, corrupt], append=True)
        assert rp._results[0].filename == "corrupt.mov"

    def test_attention_banner_reports_critical_split(self):
        from frameguard.gui import ResultsPanel
        rp = ResultsPanel()
        corrupt = QAResult("corrupt.mov")
        corrupt.add_fail("FFprobe", "unreadable")
        corrupt.finalize()
        mild = _finalized("mild.mov", "fail")
        rp.set_results([corrupt, mild], append=True)
        assert "critical" in rp._attention_banner.text().lower()

    def test_check_severity_warn_counts_half(self):
        assert check_severity("Frame Rate", "FAIL", 100) == \
            pytest.approx(2 * check_severity("Frame Rate", "WARN", 100))

    def test_result_severity_returns_top_check(self):
        r = QAResult("x.mov")
        r.add_warn("Frame Rate", "off", confidence=80)
        r.add_fail("Decode Integrity", "errors", confidence=90)
        score, top = result_severity(r)
        assert top == "Decode Integrity"
        assert score > 0


# ---------------------------------------------------------------------------
# Phase D: macOS / packaging
# ---------------------------------------------------------------------------

class TestMacHardening:
    def test_manifest_honours_target_arch_env(self, monkeypatch, tmp_path):
        """Cross-building arm64 on x86_64 must not poison the manifest with
        the host arch, or the updater applies Intel builds on Apple Silicon."""
        import importlib.util
        import os as _os
        repo = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        spec = importlib.util.spec_from_file_location(
            "fgmanifest_arch", _os.path.join(repo, "frameguard", "manifest.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        # Small tree: hashing the whole checkout would walk multi-GB build
        # outputs on a network mount and stall the suite for minutes.
        (tmp_path / "FrameGuard.exe").write_bytes(b"fake")
        monkeypatch.setenv("FRAMEGUARD_TARGET_ARCH", "arm64")
        manifest = m.build_manifest(str(tmp_path))
        assert manifest["arch"] == "arm64"

    def test_public_updates_do_not_use_a_git_payload_channel(self):
        """GitHub rejects the bundled tools as Git objects over 100 MB;
        public updates must use complete GitHub Release assets instead."""
        from frameguard import updater
        assert updater.DEFAULT_UPDATE_CHANNEL == ""
        assert updater.get_update_channel_for("darwin") == ""
        assert updater.GITHUB_RELEASES_URL.startswith("https://api.github.com/")

    def test_updater_restored_last_check_method(self):
        """check_channel_for_delta used to swallow a get_last_check_time def —
        dead code after `return True`."""
        from frameguard.updater import UpdateManager
        assert callable(getattr(UpdateManager, "get_last_check_time", None))

    def test_build_mac_signs_inside_out(self):
        import os as _os
        repo = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        sh = open(_os.path.join(repo, "build_mac.sh"), encoding="utf-8").read()
        assert "codesign --force --deep --sign" not in sh, \
            "deprecated codesign --deep still present"
        assert "--options runtime" in sh
        assert "--timestamp" in sh
        assert "notarytool" in sh
        assert "FrameGuard-${TARGET_ARCH}.dmg" in sh

    def test_swap_script_signs_inside_out(self):
        import os as _os
        repo = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        src = open(_os.path.join(repo, "frameguard", "deltaupdate.py"),
                   encoding="utf-8").read()
        assert "--deep --timestamp=none" not in src
        assert "--sign -" in src  # ad-hoc re-sign retained

    def test_requirements_are_pinned(self):
        import os as _os
        import re
        repo = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        body = open(_os.path.join(repo, "requirements.txt"), encoding="utf-8").read()
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            assert re.search(r"[<>=!~]", line), f"unpinned requirement: {line}"
