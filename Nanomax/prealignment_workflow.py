from dataclasses import dataclass, replace

from Nanomax.open_loop_panel import run_probe_prealignment
from Nanomax.prealign_panel import run_sample_prealignment


@dataclass
class NanoMaxPrealignmentResult:
    sample_result: object = None
    probe_result: object = None
    start_panel: str = None
    next_action: str = "start"


def run_nanomax_prealignment(
    sample_stage=None,
    sample_config=None,
    probe_stage=None,
    probe_config=None,
    initial_panel="sample",
    log_callback=None,
    status_provider=None,
    display_params=None,
):
    """
    Run the pre-acquisition control phase in the same process as PAM_Main_Nanomax.

    The operator can switch between the closed-loop sample panel and the open-loop
    probe panel with ':' commands. Acquisition starts only when either panel
    returns the 'start' action, so trajectory generation can use the final
    selected positions rather than stale startup values.
    """
    sample_result = None
    probe_result = None
    display_params = display_params or {}
    has_sample = sample_stage is not None and sample_config is not None
    has_probe = probe_stage is not None and probe_config is not None
    if not has_sample and not has_probe:
        return NanoMaxPrealignmentResult()

    active = str(initial_panel).strip().lower()
    if active not in ("sample", "probe"):
        active = "sample" if has_sample else "probe"
    if active == "sample" and not has_sample:
        active = "probe"
    if active == "probe" and not has_probe:
        active = "sample"

    while True:
        if active == "sample":
            config = replace(sample_config, allow_probe_switch=has_probe)
            sample_result = run_sample_prealignment(
                sample_stage,
                config,
                log_callback=log_callback,
                status_provider=status_provider,
                display_params=display_params,
            )
            if getattr(sample_result, "next_action", "start") == "probe" and has_probe:
                display_params = dict(display_params)
                display_params["SAMPLE_SCAN_READY"] = "YES" if getattr(sample_result, "scan_ok", False) else "NO"
                display_params["SAMPLE_SCAN_ERROR"] = getattr(sample_result, "scan_error", "")
                active = "probe"
                continue
            next_action = getattr(sample_result, "next_action", "start")
            return NanoMaxPrealignmentResult(
                sample_result=sample_result,
                probe_result=probe_result,
                start_panel="sample",
                next_action=next_action,
            )

        config = replace(probe_config, allow_sample_switch=has_sample)
        probe_result = run_probe_prealignment(
            probe_stage,
            config,
            log_callback=log_callback,
            status_provider=status_provider,
            display_params=display_params,
        )
        if getattr(probe_result, "next_action", "start") == "sample" and has_sample:
            active = "sample"
            continue
        next_action = getattr(probe_result, "next_action", "start")
        return NanoMaxPrealignmentResult(
            sample_result=sample_result,
            probe_result=probe_result,
            start_panel="probe",
            next_action=next_action,
        )
