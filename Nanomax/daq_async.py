import threading
import time


class BackgroundDaqInit:
    def __init__(self, factory, log_callback=None):
        self.factory = factory
        self.log = log_callback or (lambda *args, **kwargs: None)
        self.status = "not_started"
        self.step = "-"
        self.message = "DAQ init not started."
        self.started_at = None
        self.finished_at = None
        self.daq = None
        self.error = None
        self.timings = {}
        self._thread = None
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if self._thread is not None:
                return
            self.status = "running"
            self.step = "queued"
            self.message = "DAQ background initialization queued."
            self.started_at = time.time()
            self._thread = threading.Thread(target=self._run, name="PAM-DAQ-init", daemon=True)
            self._thread.start()

    def _set_step(self, step, message):
        with self._lock:
            self.step = step
            self.message = message
        self.log("DAQ_INIT_STEP_BEGIN", step=step, elapsed_s=f"{self.elapsed_s():.3f}")

    def _finish_step(self, step, start_time):
        duration = time.time() - start_time
        with self._lock:
            self.timings[step] = duration
        self.log("DAQ_INIT_STEP_DONE", step=step, duration_s=f"{duration:.3f}", elapsed_s=f"{self.elapsed_s():.3f}")

    def _run(self):
        try:
            self.log("DAQ_INIT_BACKGROUND_BEGIN")
            daq_holder = {}

            step_start = time.time()
            self._set_step("create_system", "Creating AlazarNPTSystem...")
            daq_holder["daq"] = self.factory("create_system", None)
            self._finish_step("create_system", step_start)

            step_start = time.time()
            self._set_step("configure_board", "Configuring Alazar board...")
            self.factory("configure_board", daq_holder["daq"])
            self._finish_step("configure_board", step_start)

            step_start = time.time()
            self._set_step("prepare_acquisition", "Preparing acquisition buffers...")
            self.factory("prepare_acquisition", daq_holder["daq"])
            self._finish_step("prepare_acquisition", step_start)

            with self._lock:
                self.daq = daq_holder["daq"]
                self.status = "ready"
                self.step = "ready"
                self.finished_at = time.time()
                self.message = "DAQ ready."
            self.log("DAQ_INIT_BACKGROUND_DONE", elapsed_s=f"{self.elapsed_s():.3f}")
        except Exception as exc:
            with self._lock:
                self.error = exc
                self.status = "error"
                self.step = "error"
                self.finished_at = time.time()
                self.message = f"DAQ init failed: {exc}"
            self.log("DAQ_INIT_BACKGROUND_ERROR", error=repr(exc), elapsed_s=f"{self.elapsed_s():.3f}")

    def snapshot(self):
        with self._lock:
            elapsed = self.elapsed_s()
            timings = dict(self.timings)
            return {
                "status": self.status,
                "step": self.step,
                "message": self.message,
                "elapsed_s": elapsed,
                "timings": timings,
                "error": repr(self.error) if self.error is not None else "",
            }

    def elapsed_s(self):
        if self.started_at is None:
            return 0.0
        end_time = self.finished_at if self.finished_at is not None else time.time()
        return max(0.0, end_time - self.started_at)

    def result(self):
        thread = self._thread
        if thread is not None:
            thread.join()
        if self.error is not None:
            raise self.error
        return self.daq
