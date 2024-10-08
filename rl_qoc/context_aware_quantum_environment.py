"""
Class to generate a RL environment suitable for usage with Gym and PyTorch, leveraging Qiskit modules to simulate
quantum system (could also include QUA code in the future)

Author: Arthur Strauss
Created on 26/06/2023
"""

from itertools import product
import sys
from typing import Dict, Optional, List, Any, TypeVar, SupportsFloat, Union

import numpy as np
from gymnasium.spaces import Box

# Qiskit imports
from qiskit import transpile
from qiskit.circuit import (
    QuantumCircuit,
    QuantumRegister,
    ParameterVector,
    CircuitInstruction,
    Qubit,
)
from qiskit.providers import BackendV2
from qiskit.quantum_info import (
    state_fidelity,
    Statevector,
    average_gate_fidelity,
    Operator,
)
from qiskit.transpiler import Layout
from qiskit_aer.backends import AerSimulator
from qiskit_aer.backends.aerbackend import AerBackend
from qiskit_aer.noise import NoiseModel
from qiskit_dynamics import DynamicsBackend
from qiskit_ibm_runtime import EstimatorV2

from .helper_functions import (
    projected_state,
    get_instruction_timings,
    retrieve_neighbor_qubits,
)
from .qconfig import QEnvConfig
from .base_q_env import (
    GateTarget,
    BaseQuantumEnvironment,
    QiskitBackendInfo,
)
from rl_qoc.custom_jax_sim import JaxSolver

import logging

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s INFO %(message)s",  # hardcoded INFO level
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)

ObsType = TypeVar("ObsType")
ActType = TypeVar("ActType")


def create_array(circ_trunc, batchsize, n_actions):
    arr = np.empty((circ_trunc,), dtype=object)
    for i in range(circ_trunc):
        arr[i] = np.zeros((i + 1, batchsize, n_actions))
    return arr


def target_instruction_timings(
    circuit_context: QuantumCircuit, target_instruction: CircuitInstruction
) -> tuple[List[int], List[int]]:
    """
    Return the timings of the target instructions in the circuit context
    """
    try:
        op_start_times = circuit_context.op_start_times
    except AttributeError:
        op_start_times = get_instruction_timings(circuit_context)

    target_instruction_timings = []
    for i, instruction in enumerate(circuit_context.data):
        if instruction == target_instruction:
            target_instruction_timings.append(op_start_times[i])
    return op_start_times, target_instruction_timings


class ContextAwareQuantumEnvironment(BaseQuantumEnvironment):

    def __init__(
        self,
        training_config: QEnvConfig,
        circuit_context: QuantumCircuit,
        training_steps_per_gate: Union[List[int], int] = 1500,
        intermediate_rewards: bool = False,
    ):
        """
        Class for wrapping a quantum environment enabling the calibration of a gate in a context-aware manner, that
        is with respect to an input circuit context. The input circuit context is assumed to have been transpiled and
        scheduled, and the target gate to be calibrated is assumed to be present in this circuit context. The class will
        look for the locations of the target gates in the circuit context and will enable the writing of new parametrized
        circuits, where each gate instance is replaced by a custom gate defined by the Callable parametrized_circuit_func.

        Args:
            training_config:
            circuit_context:
            training_steps_per_gate:
            intermediate_rewards:
        """
        self._training_steps_per_gate = training_steps_per_gate
        self._intermediate_rewards = intermediate_rewards
        self._circuit_context = circuit_context
        self._unbound_circuit_context = circuit_context.copy()
        self.circ_tgt_register = QuantumRegister(
            bits=[
                self._circuit_context.qubits[i] for i in training_config.physical_qubits
            ],
            name="tgt",
        )
        self.target_instruction = CircuitInstruction(
            training_config.target["gate"], (qubit for qubit in self.circ_tgt_register)
        )
        if self.tgt_instruction_counts == 0:
            raise ValueError("Target gate not found in circuit context")
        # Store time and instruction indices where target gate is played in circuit
        self._op_start_times, self._target_instruction_timings = (
            target_instruction_timings(self._circuit_context, self.target_instruction)
        )

        self._parameters = [
            ParameterVector(f"a_{j}", training_config.n_actions)
            for j in range(self.tgt_instruction_counts)
        ]
        self._param_values = create_array(
            self.tgt_instruction_counts,
            training_config.batch_size,
            training_config.action_space.shape[-1],
        )

        super().__init__(training_config)

        self.observation_space = Box(
            low=np.array([0, 0]), high=np.array([1, 1]), dtype=np.float32
        )

    def define_target_and_circuits(self):
        """
        Define target gate and circuits for calibration
        """
        if self.circuit_context.parameters:
            raise ValueError("Circuit context still contains unassigned parameters")
        assert "gate" in self.config.target, "Target should be a gate"

        if self.backend_info.coupling_map.size() == 0 and self.backend is None:
            self.backend_info.num_qubits = self._circuit_context.num_qubits
            self._physical_neighbor_qubits = retrieve_neighbor_qubits(
                self.backend_info.coupling_map, self.physical_target_qubits
            )
            self._physical_next_neighbor_qubits = retrieve_neighbor_qubits(
                self.backend_info.coupling_map,
                self.physical_target_qubits + self.physical_neighbor_qubits,
            )

        # Build registers for all relevant qubits
        # Target qubits
        tgt_register = QuantumRegister(len(self.physical_target_qubits), name="tgt")
        layouts = [
            Layout(
                {
                    tgt_register[i]: self.physical_target_qubits[i]
                    for i in range(tgt_register.size)
                }
            )
            for _ in range(self.tgt_instruction_counts)
        ]

        circ_nn_qubits = [
            self._circuit_context.qubits[i] for i in self.physical_neighbor_qubits
        ]  # Nearest neighbors
        circ_anc_qubits = [
            self._circuit_context.qubits[i] for i in self.physical_next_neighbor_qubits
        ]  # Next neighbors

        nn_register = QuantumRegister(
            len(circ_nn_qubits), name="nn"
        )  # Nearest neighbors (For new circuits)
        anc_register = QuantumRegister(
            len(circ_anc_qubits), name="anc"
        )  # Next neighbors (For new circuits)

        # Create mapping between circuit context qubits and custom circuit associated single qubit registers
        mapping = {
            circ_reg[i]: new_reg[i]
            for circ_reg, new_reg in zip(
                [self.circ_tgt_register, circ_nn_qubits, circ_anc_qubits],
                [tgt_register, nn_register, anc_register],
            )
            for i in range(len(circ_reg))
        }

        # Initialize custom and baseline circuits for each target gate (by default only contains target qubits)
        custom_circuits = [
            QuantumCircuit(tgt_register, name=f"c_circ" + str(i))
            for i in range(self.tgt_instruction_counts)
        ]
        baseline_circuits = [
            QuantumCircuit(tgt_register, name=f"b_circ" + str(i))
            for i in range(self.tgt_instruction_counts)
        ]
        # Build sub-circuit contexts: each circuit goes until target gate and preserves nearest neighbor operations

        for i in range(self.tgt_instruction_counts):  # Loop over target gates
            counts = 0
            for start_time, instruction in zip(
                self._op_start_times, self._circuit_context.data
            ):  # Loop over instructions in circuit context

                # Check if instruction involves target or nearest neighbor qubits
                involves_target_qubits = any(
                    [
                        qubit in reg
                        for reg in [self.circ_tgt_register, circ_nn_qubits]
                        for qubit in instruction.qubits
                    ]
                )
                if involves_target_qubits:
                    involved_qubits = [
                        qubit
                        for qubit in instruction.qubits
                        if qubit not in self.circ_tgt_register
                    ]
                else:
                    involved_qubits = []

                # If instruction involves target or nn qubits and happens before target gate, add it to circuit

                if (
                    counts <= i or start_time <= self._target_instruction_timings[i]
                ) and involves_target_qubits:
                    for qubit in involved_qubits:
                        q_reg = nn_register if qubit in circ_nn_qubits else anc_register
                        physical_qubits = (
                            self.physical_neighbor_qubits
                            if qubit in circ_nn_qubits
                            else self.physical_next_neighbor_qubits
                        )
                        if (
                            mapping[qubit] not in custom_circuits[i].qubits
                        ):  # Add register if not already added
                            baseline_circuits[i].add_bits([mapping[qubit]])
                            custom_circuits[i].add_bits([mapping[qubit]])

                            layouts[i].add(
                                mapping[qubit],
                                physical_qubits[q_reg.index(mapping[qubit])],
                            )

                    baseline_circuits[i].append(
                        instruction.operation, [mapping[q] for q in instruction.qubits]
                    )
                    if instruction != self.target_instruction:
                        custom_circuits[i].append(
                            instruction.operation,
                            [mapping[q] for q in instruction.qubits],
                        )
                    else:  # Add custom instruction in place of target gate
                        try:
                            self.parametrized_circuit_func(
                                custom_circuits[i],
                                self.parameters[counts],
                                tgt_register,
                                **self._func_args,
                            )
                        except TypeError:
                            raise TypeError("Failed to call parametrized_circuit_func")
                        counts += 1

        target = [
            GateTarget(
                self.config.target["gate"],
                self.physical_target_qubits,
                self.config.n_reps,
                baseline_circuit,
                tgt_register,
                layout,
                **self.config.reward_config.reward_args,
            )
            for baseline_circuit, layout in zip(baseline_circuits, layouts)
        ]
        return target, custom_circuits, baseline_circuits

    def reset(
        self,
        *,
        seed: Optional[int] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> tuple[ObsType, dict[str, Any]]:
        """Reset the Environment, chooses a new input state"""
        super().reset(seed=seed)

        self._param_values = create_array(
            self.tgt_instruction_counts, self.batch_size, self.action_space.shape[0]
        )
        self._inside_trunc_tracker = 0
        return self._get_obs(), self._get_info()

    def step(
        self, action: ActType
    ) -> tuple[ObsType, SupportsFloat, bool, bool, dict[str, Any]]:
        # trunc_index tells us which circuit truncation should be trained
        # Dependent on global_step and method select_trunc_index
        trunc_index = self.trunc_index
        # Figure out if in middle of param loading or should compute the final reward (step_status < trunc_index or ==)
        step_status = self._inside_trunc_tracker
        self._step_tracker += 1

        if self._episode_ended:
            terminated = True
            return (
                self.reset()[0],
                np.zeros(self.batch_size),
                terminated,
                False,
                self._get_info(),
            )

        if trunc_index >= self.tgt_instruction_counts:
            # raise IndexError(f"Circuit does contain only {self.tgt_instruction_counts} target gates and step"
            #                  f" function tries to access gate nb {trunc_index} ")
            truncated = True
            return (
                self.reset()[0],
                np.zeros(self.batch_size),
                False,
                truncated,
                self._get_info(),
            )

        params, batch_size = np.array(action), len(np.array(action))
        if batch_size != self.batch_size:
            raise ValueError(
                f"Action batch size {batch_size} does not match environment batch size {self.batch_size}"
            )
        self._param_values[trunc_index][step_status] = params
        params = np.reshape(
            np.vstack([param_set for param_set in self._param_values[trunc_index]]),
            (self.batch_size, (trunc_index + 1) * self.action_space.shape[-1]),
        )
        if step_status < trunc_index:  # Intermediate step within the circuit truncation
            self._inside_trunc_tracker += 1
            terminated = False

            if self._intermediate_rewards:
                reward = self.perform_action(params)
                obs = reward  # Set observation to obtained reward (might not be the smartest choice here)
                return obs, reward, terminated, False, self._get_info()
            else:
                return (
                    self._get_obs(),
                    np.zeros(batch_size),
                    terminated,
                    False,
                    self._get_info(),
                )

        else:
            terminated = self._episode_ended = True
            reward = self.perform_action(params)
            if self._intermediate_rewards:
                obs = reward
            else:
                obs = self._get_obs()

            # Using Negative Log Error as the Reward
            if np.mean(reward) > self._max_return:
                self._max_return = np.mean(reward)
                self._optimal_action = self.mean_action
            self.reward_history.append(reward)
            assert (
                len(reward) == self.batch_size
            ), f"Reward table size mismatch {len(reward)} != {self.batch_size} "
            assert not np.any(np.isinf(reward)) and not np.any(
                np.isnan(reward)
            ), "Reward table contains NaN or Inf values"
            optimal_error_precision = 1e-6
            max_fidelity = 1.0 - optimal_error_precision
            reward = np.clip(reward, a_min=0.0, a_max=max_fidelity)
            reward = -np.log(1.0 - reward)

            return obs, reward, terminated, False, self._get_info()

    def _get_obs(self):
        if isinstance(self.target, GateTarget) and self.config.reward_method == "state":
            return np.array(
                [
                    self._index_input_state / len(self.target.input_states),
                    self._target_instruction_timings[self._inside_trunc_tracker],
                ]
                + list(self._observable_to_observation())
            )
        else:
            return np.array(
                [0, self._target_instruction_timings[self._inside_trunc_tracker]]
            )

    def compute_benchmarks(self, qc: QuantumCircuit, params: np.array) -> np.array:
        """
        Method to store in lists all relevant data to assess performance of training (fidelity information)
        :param params: Batch of actions
        """

        if (
            self.config.check_on_exp
        ):  # Perform real experiments to retrieve from measurement data fidelities
            # Assess circuit fidelity with ComputeUncompute algo
            try:
                print("Starting Direct Fidelity Estimation...")
                observables, shots = self.retrieve_observables(
                    self._input_state.target_state,
                    self.circuits[self.trunc_index],
                    self.config.benchmark_config.dfe_precision,
                )
                if self.abstraction_level == "circuit":
                    qc = self.backend_info.custom_transpile(
                        qc,
                        initial_layout=self.layout,
                        scheduling=False,
                    )
                pubs = [
                    (
                        qc,
                        obs.apply_layout(qc.layout),
                        [self.mean_action],
                        1 / np.sqrt(shot),
                    )
                    for obs, shot in zip(
                        observables.group_commuting(qubit_wise=True), shots
                    )
                ]
                if isinstance(self.estimator, EstimatorV2):
                    self.estimator.options.update(
                        job_tags=[f"DFE_step{self._step_tracker}"]
                    )
                job = self.estimator.run(pubs=pubs)
                results = job.result()
                circuit_fidelities = np.sum(
                    [result.data.evs for result in results], axis=0
                ) / len(observables)
                print("Finished DFE")
                return circuit_fidelities
            except Exception as exc:
                self.close()
                raise exc

        else:  # Perform simulation at circuit or pulse level
            print("Starting simulation benchmark...")
            if not self.config.reward_method == "fidelity":
                params = np.array(
                    [self.mean_action]
                )  # Benchmark policy only through mean action
            if self.abstraction_level == "circuit":
                fids = self.simulate_circuit(qc, params)
            else:  # Pulse simulation
                fids = self.simulate_pulse_circuit(qc, params)
            print("Avg gate fidelity:", self.avg_fidelity_history[-1])
            print("Finished simulation benchmark")
            return fids

    @property
    def parameters(self) -> List[ParameterVector]:
        return self._parameters

    @property
    def fidelity_history(self):
        return self.avg_fidelity_history

    @property
    def tgt_instruction_counts(self) -> int:
        """
        Return number of target instructions present in circuit context
        """
        return self.circuit_context.data.count(self.target_instruction)

    @property
    def target(self) -> GateTarget:
        """
        Return current target to be calibrated
        """
        return self._target[self.trunc_index]

    def get_target(self, trunc_index: Optional[int] = None):
        """
        Return target to be calibrated at given truncation index.
        If no index is provided, return list of all targets.

        Args:
            trunc_index: Index of truncation to return target for.
        """
        return self._target[trunc_index] if trunc_index is not None else self._target

    @property
    def trunc_index(self) -> int:
        if self._intermediate_rewards:
            return self.step_tracker % self.tgt_instruction_counts
        else:
            return np.min(
                [
                    self._step_tracker // self.training_steps_per_gate,
                    self.tgt_instruction_counts - 1,
                ]
            )

    @property
    def training_steps_per_gate(self) -> int:
        return self._training_steps_per_gate

    @training_steps_per_gate.setter
    def training_steps_per_gate(self, nb_of_steps: int):
        try:
            assert nb_of_steps > 0 and isinstance(nb_of_steps, int)
            self._training_steps_per_gate = nb_of_steps
        except AssertionError:
            raise ValueError("Training steps number should be positive integer.")

    def episode_length(self, global_step: int) -> int:
        # assert (
        #         global_step == self.step_tracker
        # ), "Given step not synchronized with internal environment step counter"
        return 1 + self.trunc_index

    def clear_history(self) -> None:
        """Reset all counters related to training"""
        super().clear_history()

    @property
    def unbound_circuit_context(self) -> QuantumCircuit:
        """
        Return the unbound circuit context (relevant when circuit context is parameterized)
        """
        return self._unbound_circuit_context

    def set_unbound_circuit_context(self, new_context: QuantumCircuit, **kwargs):
        """
        Update the unbound circuit context
        Keyword arguments can be used to assign values to parameters in the new context for immediate target calculation

        """
        self._unbound_circuit_context = new_context
        self.set_circuit_context(new_context.copy(), **kwargs)
        if self._circuit_context.parameters:
            raise ValueError("Unbound circuit context still contains parameters")

    @property
    def circuit_context(self) -> QuantumCircuit:
        """
        Return the current circuit context
        """
        return self._circuit_context

    def set_circuit_context(
        self,
        new_context: Optional[QuantumCircuit] = None,
        backend: Optional[BackendV2] = None,
        **kwargs,
    ):
        """
        Update the circuit context and all relevant attributes
        """
        if new_context is not None:  # Update circuit context from scratch
            self._circuit_context = new_context
            # Define target register and nearest neighbor register for truncated circuits
            self.circ_tgt_register = QuantumRegister(
                bits=[
                    self._circuit_context.qubits[i] for i in self.physical_target_qubits
                ],
                name="tgt",
            )

            # Adjust target register to match it with circuit context
            self.target_instruction = CircuitInstruction(
                self.target.gate, (qubit for qubit in self.circ_tgt_register)
            )
            if self.tgt_instruction_counts == 0:
                raise ValueError("Target gate not found in circuit context")

            self._parameters = [
                ParameterVector(f"a_{j}", self.n_actions)
                for j in range(self.tgt_instruction_counts)
            ]

            self._op_start_times, self._target_instruction_timings = (
                target_instruction_timings(
                    self._circuit_context, self.target_instruction
                )
            )

            self._target, self.circuits, self.baseline_circuits = (
                self.define_target_and_circuits()
            )

            self._param_values = create_array(
                self.tgt_instruction_counts,
                self.batch_size,
                self.action_space.shape[-1],
            )

        else:
            for param in kwargs:
                if self._circuit_context.has_parameter(param):
                    self._circuit_context.assign_parameters(
                        {param: kwargs[param]}, inplace=True
                    )
                else:
                    raise ValueError(f"Parameter {param} not found in circuit context")
            self._target, self.circuits, self.baseline_circuits = (
                self.define_target_and_circuits()
            )

        if backend is not None:
            self.backend = backend
            self.backend_info = QiskitBackendInfo(
                backend, self.config.backend_config.instruction_durations_dict
            )
            self._physical_neighbor_qubits = retrieve_neighbor_qubits(
                self.backend_info.coupling_map, self.physical_target_qubits
            )
            self._physical_next_neighbor_qubits = retrieve_neighbor_qubits(
                self.backend_info.coupling_map,
                self.physical_target_qubits + self.physical_neighbor_qubits,
            )
