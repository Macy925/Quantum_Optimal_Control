from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable, List, Sequence, Dict, Union, Tuple

from quam.components import Channel as QuAMChannel
from quam.components.pulses import Pulse as QuAMPulse
from quam import quam_dataclass

from qiskit.circuit import ParameterExpression, QuantumCircuit
from qiskit.providers import BackendV2 as Backend, QubitProperties
from qiskit.pulse import (
    ScheduleBlock,
    Schedule,
    ControlChannel,
    DriveChannel,
    MeasureChannel,
    AcquireChannel,
    Play,
    ShiftPhase,
    ShiftFrequency,
    Delay,
    UnassignedDurationError,
    PulseError,
)
from qiskit.transpiler import Target, InstructionProperties
from qiskit.qasm3 import dumps as qasm3_dumps
from qiskit.pulse.transforms import block_to_schedule
from qiskit.pulse.channels import Channel as QiskitChannel
from qiskit.pulse.library import SymbolicPulse
from qiskit.pulse.library.waveform import Waveform
from qm.jobs.running_qm_job import RunningQmJob
from qm.jobs.pending_job import QmPendingJob, QmJob
from qm.qua import *
from qm import QuantumMachinesManager, Program
from qualang_tools.addons.variables import assign_variables_to_element
from quam_components.quam import QuAM
from qualang_tools.video_mode import ParameterTable
from oqc import (
    Compiler,
    HardwareConfig,
    OperationIdentifier,
    OperationsMapping,
    QubitsMapping,
)

_real_time_parameters = {
    "amp",
    "angle",
    "frequency",
}  # Parameters that can be used in real-time
_ref_amp = 0.1
_ref_phase = 0.0


# TODO: Add duration to the list of real-time parameters (need ScheduleBlock to QUA compiler)
class FluxChannel(QiskitChannel):
    prefix = "f"


@quam_dataclass
class QuAMQiskitPulse(QuAMPulse):
    def __init__(self, pulse: SymbolicPulse | Waveform):
        self.pulse: SymbolicPulse | Waveform = pulse
        super().__init__(
            length=self.pulse.duration if not self.pulse.is_parameterized() else 0,
            id=pulse.name,
        )

    def waveform_function(
        self,
    ) -> Union[
        float,
        complex,
        List[float],
        List[complex],
        Tuple[float, float],
        Tuple[List[float], List[float]],
    ]:
        if isinstance(self.pulse, Waveform):
            return self.pulse.samples.tolist()
        elif isinstance(self.pulse, SymbolicPulse):
            try:
                return self.pulse.get_waveform().samples.tolist()
            except (AttributeError, PulseError) as e:
                raise PulseError(
                    "Pulse waveform could not be retrieved from the given pulse"
                ) from e

    def is_parametrized(self):
        return self.pulse.is_parametrized()

    def is_compile_time_parametrized(self):
        """
        Check if the pulse is parametrized with compile-time parameters
        """
        return any(
            isinstance(self.pulse.parameters[param], ParameterExpression)
            and param not in _real_time_parameters
            for param in self.pulse.parameters
        )

    def is_real_time_parametrized(self):
        """
        Check if the pulse is parametrized with real-time parameters
        """
        return any(
            isinstance(self.pulse.parameters[param], ParameterExpression)
            for param in _real_time_parameters
        )


class QMProvider:
    def __init__(self, host, port, cluster_name, octave_config):
        """
        Qiskit Provider for the Quantum Orchestration Platform (QOP)
        Args:
            host: The host of the QOP
            port: The port of the QOP
            cluster_name: The name of the cluster
            octave_config: The octave configuration
        """
        super().__init__(self)
        self.qmm = QuantumMachinesManager(
            host=host, port=port, cluster_name=cluster_name, octave=octave_config
        )

    def get_backend(self, quam: QuAM):
        return QMBackend(self, quam)

    def backends(self, name=None, filters=None, **kwargs):
        raise NotImplementedError("Not implemented yet")

    def __str__(self):
        pass

    def __repr__(self):
        pass


class QMBackend(Backend, ABC):
    def __init__(
        self,
        machine: QuAM,
    ):
        Backend.__init__(self, name="QUA backend")
        self._target = Target(
            description="QUA target",
            dt=1e-9,
            granularity=4,
            num_qubits=len(machine.qubits),
            min_length=16,
        )
        self.machine = machine
        self._pulse_to_quam_channels: Dict[QiskitChannel, QuAMChannel] = {}
        self._quam_to_pulse_channels: Dict[
            QuAMChannel, QiskitChannel | Sequence[QiskitChannel]
        ] = {}
        self._operation_mapping_QUA: OperationsMapping = {}
        self.populate_target(machine)

    @property
    def target(self):
        return self._target

    @abstractmethod
    @property
    def qubit_mapping(self) -> QubitsMapping:
        """
        Build the qubit to quantum elements mapping for the backend.
        Should be of the form {qubit_index: (quantum_element1, quantum_element2, ...)}
        """
        pass

    @property
    def max_circuits(self):
        return None

    @classmethod
    def _default_options(cls):
        pass

    @abstractmethod
    def populate_target(self, machine: QuAM):
        """
        Populate the target instructions with the QOP configuration (currently hardcoded for
        Transmon based QuAM architecture)

        """
        pass

    def get_quam_channel(self, channel: QiskitChannel):
        """
        Convert a Qiskit Pulse channel to a QuAM channel

        Args:
            channel: The Qiskit Pulse Channel to convert

        Returns:
            The corresponding QuAM channel
        """
        return self._pulse_to_quam_channels[channel]

    def get_pulse_channel(self, channel: QuAMChannel):
        """
        Convert a QuAM channel to a Qiskit Pulse channel

        Args:
            channel: The QuAM channel to convert

        Returns:
            The corresponding pulse channel
        """
        return self._quam_to_pulse_channels[channel]

    def meas_map(self) -> List[List[int]]:
        return self._target.concurrent_measurements

    def drive_channel(self, qubit: int):
        """
        Get the drive channel for a given qubit (should be mapped to a quantum element in configuration)
        """
        return DriveChannel(qubit)

    def control_channel(self, qubits: Iterable[int]):
        pass

    def measure_channel(self, qubit: int):
        return MeasureChannel(qubit)

    def acquire_channel(self, qubit: int):
        return AcquireChannel(qubit)

    def flux_channel(self, qubit: int):
        return FluxChannel(qubit)

    def run(self, run_input, **options):
        """
        Run a QuantumCircuit on the QOP backend
        Args:
            run_input: The QuantumCircuit to run
            options: The options for the run
        """
        if isinstance(run_input, Sequence):
            qua_progs = []
            for qc in run_input:
                qua_prog = self.qua_prog_from_qc(qc)
                qua_progs.append(qua_prog)

    def schedule_to_qua_macro(self, sched: Schedule):

        def qua_macro(*params):
            param_counter = 0
            time_tracker = {channel: 0 for channel in sched.channels}
            for time, instruction in sched.instructions:
                try:
                    qiskit_channel = instruction.channel
                except AttributeError:
                    raise AttributeError(
                        "Provided instruction not compatible with QUA conversion"
                    )
                if qiskit_channel.is_parametrized():
                    # TODO: Implement parametrized channels
                    raise NotImplementedError(
                        "Parametrized channels are not supported yet"
                    )
                quam_channel = self.get_quam_channel(qiskit_channel)

                if time_tracker[qiskit_channel] < time:
                    quam_channel.wait((time - time_tracker[qiskit_channel]))

                if isinstance(instruction, Play):
                    param_counter = self.qiskit_to_qua_play(
                        quam_channel, instruction, params, param_counter
                    )
                elif isinstance(instruction, ShiftPhase):
                    if instruction.is_parameterized():
                        phase = params[param_counter]
                        param_counter += 1
                    else:
                        phase = instruction.phase
                    quam_channel.frame_rotation(phase)

                elif isinstance(instruction, ShiftFrequency):
                    if instruction.is_parameterized():
                        freq = params[param_counter]
                        param_counter += 1
                    else:
                        freq = instruction.frequency
                    quam_channel.update_frequency(freq)

                elif isinstance(instruction, Delay):
                    quam_channel.wait(instruction.duration)
                else:
                    raise ValueError(f"Unknown instruction {instruction}")

        return qua_macro

    def quantum_circuit_to_qua(self, qc: QuantumCircuit):
        """
        Convert a QuantumCircuit to a QUA program
        """

        basis_gates = self.operation_names.copy()

        if qc.calibrations:  # Check for custom calibrations
            for gate_name, cal_info in qc.calibrations.items():
                if gate_name not in basis_gates:  # Make it a basis gate for OQ compiler
                    basis_gates.append(gate_name)
                for (qubits, parameters), schedule in cal_info.items():
                    if not isinstance(schedule, Schedule):
                        raise ValueError(
                            f"Calibration schedule for {gate_name} is not a Schedule"
                        )
                    parametrized_channels_count = 0
                    for channel in schedule.channels:
                        if channel.is_parameterized():
                            parametrized_channels_count += 1
                    if parametrized_channels_count == 0:
                        parametrized_channels_count = None

                    # Update QuAM with additional pulses
                    for idx, (time, instruction) in enumerate(
                        schedule.filter(instruction_types=[Play]).instructions
                    ):
                        instruction: Play
                        pulse, channel = instruction.pulse, instruction.channel
                        pulse_name = pulse.name
                        if pulse_name in self.get_quam_channel(channel).operations:
                            pulse_name += str(pulse.id)

                        quam_pulse = QuAMQiskitPulse(pulse)
                        if quam_pulse.is_compile_time_parametrized():
                            raise ValueError(
                                "Compile-time parametrized pulses are not supported in this execution"
                                "mode."
                            )
                        if quam_pulse.is_real_time_parametrized():
                            for param in _real_time_parameters:
                                if isinstance(
                                    pulse.parameters[param], ParameterExpression
                                ):
                                    pass
                                    # TODO: Add the real-time parameters to the QuAM pulse

                        self.get_quam_channel(channel).operations[pulse.name] = (
                            QuAMQiskitPulse(pulse)
                        )

                    self._operation_mapping_QUA[
                        OperationIdentifier(
                            gate_name,
                            len(parameters),
                            qubits,
                            parametrized_channels_count,
                        )
                    ] = self.schedule_to_qua_macro(schedule)
        hardware_config = HardwareConfig(
            quantum_operations_db=self._operation_mapping_QUA,
            physical_qubits=self._qubit_mapping,
        )
        compiler = Compiler(hardware_config=hardware_config)
        open_qasm_code = qasm3_dumps(qc, includes=(), basis_gates=basis_gates)
        open_qasm_code = "\n".join(
            line
            for line in open_qasm_code.splitlines()
            if not line.strip().startswith(("barrier",))
        )
        result = compiler.compile(open_qasm_code)
        return result.result_program

    def qua_prog_from_qc(self, qc: QuantumCircuit | Schedule | ScheduleBlock | Program):
        """
        Convert given input into a QUA program
        """
        if isinstance(qc, Program):
            return qc
        elif isinstance(qc, QuantumCircuit):
            return self.quantum_circuit_to_qua(qc)
        elif isinstance(qc, ScheduleBlock):  # Convert to Schedule first
            try:
                schedule = block_to_schedule(qc)
            except (UnassignedDurationError, PulseError) as e:
                # TODO: Build ScheduleBlock to QUA compiler
                raise RuntimeError(
                    "ScheduleBlock could not be converted to Schedule (required"
                    "for converting it to QUA program"
                ) from e

            return self.schedule_to_qua_macro(schedule)
        elif isinstance(qc, Schedule):
            return self.schedule_to_qua_macro(qc)
        else:
            raise ValueError(f"Unsupported input {qc}")

    def qiskit_to_qua_play(self, quam_channel, instruction, params, param_counter):
        """
        Convert a Qiskit Play instruction to a QUA Play instruction
        """
        return param_counter


class FluxTunableTransmonBackend(QMBackend):

    def __init__(
        self,
        machine: QuAM,
    ):
        super().__init__(machine)

    @property
    def qubit_mapping(self) -> QubitsMapping:
        """
        Retrieve the qubit to quantum elements mapping for the backend.
        """
        return {
            i: (qubit.xy.name, qubit.z.name, qubit.resonator.name)
            for i, qubit in enumerate(self.machine.qubits.values())
        }

    def populate_target(self, machine: QuAM):
        """
        Populate the target instructions with the QOP configuration (currently hardcoded for
        Transmon based QuAM architecture)

        """
        for i, qubit in enumerate(machine.qubits.values()):
            self._target.qubit_properties[i] = QubitProperties(
                t1=qubit.T1, t2=qubit.T2ramsey, frequency=qubit.f_01
            )
            self._quam_to_pulse_channels[qubit.xy] = DriveChannel(i)
            self._pulse_to_quam_channels[DriveChannel(i)] = qubit.xy
            self._quam_to_pulse_channels[qubit.z] = FluxChannel(i)
            self._pulse_to_quam_channels[FluxChannel(i)] = qubit.z
            self._quam_to_pulse_channels[qubit.resonator] = [
                MeasureChannel(i),
                AcquireChannel(i),
            ]
            self._pulse_to_quam_channels[MeasureChannel(i)] = qubit.resonator
            self._pulse_to_quam_channels[AcquireChannel(i)] = qubit.resonator
            # TODO: Add the rest of the channels for QubitPairs (ControlChannels)

            # TODO: Update the instructions both in Qiskit and in the OQC operations mapping
            # TODO: Figure out if pulse calibrations should be added to Target

        self._coupling_map = self._target.build_coupling_map()


def qua_declaration(n_qubits, readout_elements):
    """
    Macro to declare the necessary QUA variables

    :param n_qubits: Number of qubits used in this experiment
    :return:
    """
    I, Q = [[declare(fixed) for _ in range(n_qubits)] for _ in range(2)]
    I_st, Q_st = [[declare_stream() for _ in range(n_qubits)] for _ in range(2)]
    # Workaround to manually assign the results variables to the readout elements
    for i in range(n_qubits):
        assign_variables_to_element(readout_elements[i], I[i], Q[i])
    return I, I_st, Q, Q_st


def get_el_from_channel(channel: QiskitChannel):
    return ""


def get_pulse_from_instruction(
    pulse_instance: Waveform | SymbolicPulse,
    channel: QiskitChannel,
    channel_mapping: dict = None,
    parameter_table: ParameterTable = None,
    pulse_lib: dict = None,
):
    param_statement = {"pulse": pulse_lib[pulse_instance.name]}

    for param_name, param_value in pulse_instance.parameters.items():
        if param_name == "amp" and isinstance(param_value, ParameterExpression):
            amp_ = parameter_table[param_value]
            angle = pulse_instance.parameters.get("angle", None)
            if isinstance(angle, ParameterExpression):
                angle = parameter_table[angle]
            elif angle == 0:
                angle = None

            matrix_elements = (
                [
                    amp_ * Math.cos(angle),
                    -amp_ * Math.sin(angle),
                    amp_ * Math.sin(angle),
                    amp_ * Math.cos(angle),
                ]
                if angle is not None
                else [amp_]
            )
            param_statement["pulse"] *= amp(*matrix_elements)

        elif param_name == "duration" and isinstance(param_value, ParameterExpression):
            param_statement["duration"] = parameter_table[param_value]

    return param_statement
