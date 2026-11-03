"""Local admission-study wiring; ordinary continuation behavior stays unchanged."""
from rsi_lab.engine.platform import PlatformContinuationDriver, continue_system
from rsi_lab.subjects.openevolve import admission_decision


class OpenEvolveAdmissionContinuationDriver(PlatformContinuationDriver):
    def _admission_decision(self, prepared):
        # Upstream validity is necessary for the controller; the bound platform
        # condition independently decides whether its proposed state is committed.
        return admission_decision(prepared)


def continue_admission_system(adapter, tasks, source_environment, directory, **kwargs):
    return continue_system(adapter, tasks, source_environment, directory,
                           driver_type=OpenEvolveAdmissionContinuationDriver, **kwargs)
