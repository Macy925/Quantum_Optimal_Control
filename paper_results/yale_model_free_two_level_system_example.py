"""
Code example reproducing Educational Example described in Appendix A of the paper PhysRevX.12.011059
 (https://doi.org/10.1103/PhysRevX.12.011059) using Qiskit modules

 Author: Arthur Strauss
 Created on 05/08/2022
"""

import tensorflow as tf
import numpy as np
from qiskit import QuantumCircuit
from qiskit_aer.backends.qasm_simulator import QasmSimulator
import qiskit.quantum_info as qi
from tensorflow.python.keras.optimizer_v2.adam import Adam
from tensorflow_probability.python.distributions import Normal
from tqdm import tqdm
from tensorflow.python.keras.optimizer_v2.gradient_descent import SGD
# from tensorflow.python.keras.losses import MSE
from scipy.stats import norm
import matplotlib.pyplot as plt
from typing import Union
import csv

"""This code sets the simplest RL algorithm (Policy Gradient) for solving a quantum control problem. The goal is the 
following: We have access to a quantum computer (here a simulator provided by IBM Q) containing one qubit. The qubit 
originally starts in the state |0>, and we would like to apply a quantum gate (operation) to bring it to the |1> 
state. To do so, we have access to a gate parametrized with an angle, and the RL agent must find the optimal angle 
that maximizes the probability of measuring the qubit in the |1> state. Optimal value for amplitude amp (angle/2π) is 
0.5. The RL agent chooses its actions (that is picks a random value for amp) by drawing a number from a Gaussian 
distribution, of mean mu and standard deviation sigma. The trainable parameters are therefore those two latter 
variables (we expect the mean to be close to 0.5 and the variance very low). The reward is a binary number obtained 
upon measurement of the circuit produced (only two possible outcomes can be measured). 

"""


def perform_action(amp: Union[tf.Tensor, np.array], shots=1, target_state="|1>"):
    """
    Execute quantum circuit with parametrized amplitude, retrieve measurement result and assign rewards accordingly
    :param amp: amplitude parameter, provided as an array of size batchsize
    :param shots: number of evaluations to be done on the quantum computer (for simplicity stays to 1)
    :param target_state: String indicating which target state is intended (can currently only be "|1>" or "|->")
    :return: Reward table (reward for each run in the batch)
    """
    global qc, qasm, seed2
    angles = np.array(amp)
    density_matrix = np.zeros([2, 2], dtype='complex128')
    assert len(np.shape(angles)) == 1, f'What happens : {np.shape(angles)}'

    reward_table = np.zeros(np.shape(angles))
    for j, angle in enumerate(angles):
        if target_state == "|1>":
            qc.rx(2 * np.pi * angle, 0)  # Add parametrized gate for each amplitude in the batch
        elif target_state == "|->":
            qc.ry(2 * np.pi * angle, 0)  # Add parametrized gate for each amplitude in the batch
            qc.h(0)  # Rotate qubit for measurement  in Hadamard basis
        # Store quantum state for fidelity estimation (not used for training the agent)
        q_state = qi.Statevector.from_instruction(qc)
        density_matrix += np.array(q_state.to_operator()) / len(angles)

        qc.measure(0, 0)  # Measure the qubit
        job = qasm.run(qc, shots=shots, seed_simulator=seed2)
        result = job.result()
        counts = result.get_counts(qc)  # Returns dictionary with keys '0' and '1' with number of counts for each key

        #  Calculate reward (Generalized to include any number of shots per each action)
        if '1' in counts and '0' in counts:
            reward_table[j] += np.mean(np.array([1] * counts['1'] + [-1] * counts['0']))
        elif '0' in counts:
            reward_table[j] += np.mean([-1] * counts['0'])
        else:
            reward_table[j] += np.mean([1] * counts['1'])
        qc.clear()  # Reset the Quantum Circuit for next iteration
    return reward_table, qi.DensityMatrix(density_matrix)  # Shape [batchsize]


# Variables to define environment
seed = 2523  # Seed for action sampling (ref 2763)
seed2 = 3000  # Seed for QASM simulator
qc = QuantumCircuit(1, 1, name="qc")  # Two-level system of interest, 1 qubit
qasm = QasmSimulator(method="statevector")  # Simulation backend (mock quantum computer)
save_data = False  # Decide if data should be saved in a csv file

target_state = {
    "|1>": qi.DensityMatrix(np.array([[0.], [1.]]) @ np.array([[0., 1.]])),
    "|->": qi.DensityMatrix(0.5 * np.array([[1.], [-1.]]) @ np.array([[1., -1.]]))
}
tgt_string = "|1>"

# Hyperparameters for the agent
insert_baseline = True  # Indicate if you want the actor-critic version (True) or simple REINFORCE (False)
use_PPO = True
concurrent_optimization = True  # Fix if optimization of actor and critic should be done by same optimizer or separately

optimizer_string = "Adam"
epsilon = 0.2  # Parameter for clipping value (PPO)
n_epochs = 50
batch_size = 50
eta = 0.5  # Learning rate for policy update step
eta_2 = 0.1  # Learning rate for critic (value function) update step
critic_loss_coeff = 0.5
log_prob_clip = 5
grad_clip = 0.1
if insert_baseline:
    if concurrent_optimization:
        # Choose optimizer of your choice by commenting irrelevant line
        if optimizer_string == "Adam":
            optimizer = Adam(learning_rate=eta)
        elif optimizer_string == "SGD":
            optimizer = SGD(learning_rate=eta)
    else:
        # Choose optimizer of your choice by commenting irrelevant line
        optimizer_actor, optimizer_critic = Adam(learning_rate=eta), Adam(learning_rate=eta_2)
        # optimizer_actor, optimizer_critic = SGD(learning_rate=eta), SGD(learning_rate=eta_2)
else:
    # Choose optimizer of your choice by commenting irrelevant line
    optimizer = Adam(learning_rate=eta)
    # optimizer = SGD(learning_rate=eta)

# Policy parameters
mu = tf.Variable(initial_value=tf.random.normal([], stddev=0.05, seed=seed), trainable=True, name="µ")
sigma = tf.Variable(initial_value=1., trainable=True, name="sigma")
sigma_eps = np.array(1e-6, dtype='float32')  # for numerical stability

# Old parameters are updated with one-step delay, necessary for PPO implementation

mu_old = tf.Variable(initial_value=mu, trainable=False)
sigma_old = tf.Variable(initial_value=sigma, trainable=False)
# Critic parameter (single state-independent baseline b)

b = tf.Variable(initial_value=0., trainable=insert_baseline, name="baseline")
#  Keep track of variables (when script will be functional, do some saving to external file)

data = {
    "means": np.zeros(n_epochs + 1),
    "stds": np.zeros(n_epochs + 1),
    "amps": np.zeros([n_epochs, batch_size]),
    "rewards": np.zeros([n_epochs, batch_size]),
    "critic_loss": np.zeros(n_epochs),
    "fidelity": np.zeros(n_epochs),
    "hyperparams": {
        "learning_rate": eta,
        "seed": seed,
        "clipping_PPO": epsilon,
        "clip_value for log_probs": log_prob_clip,
        "grad_clip_value": grad_clip,
        "n_epochs": n_epochs,
        "batch_size": batch_size,
        "target_state": (tgt_string, target_state[tgt_string]),
        "critic?": insert_baseline,
        "PPO?": use_PPO,
        "Concurrent optimization?": concurrent_optimization,
        "critic_loss_coeff": critic_loss_coeff,
        "optimizer": optimizer_string
    }
}

success = 0


def normal_distrib(amp: Union[tf.Tensor, np.array], mean: Union[tf.Variable, float], std: Union[tf.Variable, float]):
    """
    Compute probability density of Gaussian distribution evaluated at amplitude amp
    :param amp: amplitude/angle chosen by the random selection
    :param mean: mean of the Gaussian distribution from which amp was sampled
    :param std: standard deviation of the Gaussian distribution from which amp was sampled
    :return: probability density of Gaussian evaluated at amp
    """
    # return math.divide(math.exp(math.divide(math.pow(amp - mu, 2), -2 * math.pow(sigma, 2))),
    #                    math.sqrt(2 * np.pi * math.pow(sigma, 2)))
    return tf.exp(-(amp - mean) ** 2 / (2 * std ** 2 + sigma_eps)) / tf.sqrt(2 * np.pi * (std ** 2 + sigma_eps))


Normal_distrib = Normal(loc=mu, scale=tf.abs(sigma) + sigma_eps, allow_nan_stats=False)
_, log_probs = Normal_distrib.experimental_sample_and_log_prob(sample_shape=[batch_size], seed=seed)
log_probs = tf.clip_by_value(log_probs, -log_prob_clip, log_prob_clip)
log_probs_old = log_probs

for i in tqdm(range(n_epochs)):
    # Sample action from policy (Gaussian distribution with parameters mu and sigma)
    a = tf.random.normal([batch_size], mean=mu, stddev=tf.abs(sigma) + sigma_eps, seed=seed)
    Normal_distrib = Normal(loc=mu, scale=sigma, allow_nan_stats=False)
    a2 = Normal_distrib.sample([batch_size], seed=seed)
    # Run quantum circuit to retrieve rewards (in this example, only one time step)
    reward, dm_observed = perform_action(a, shots=1, target_state=tgt_string)
    reward2, dm_observed2 = perform_action(a2, shots=1, target_state=tgt_string)

    with tf.GradientTape(persistent=True) as tape:

        """
        Calculate return (to be maximized, therefore the minus sign placed in front of the loss 
        since applying gradients minimize the loss), E[R*log(proba(amp)] where proba is the gaussian
        probability density (cf paper of reference, educational example).
        In case of the PPO, loss function is slightly changed.
        """
        log_probs = Normal_distrib.log_prob(a2)
        # log_probs = tf.clip_by_value(log_probs, -log_prob_clip, log_prob_clip)
        advantage = reward - b  # If not using the critic (baseline), then b=0, and we are left with the reward
        advantage2 = reward2 - b  # If not using the critic (baseline), then b=0, and we are left with the reward
        if use_PPO:
            ratio = normal_distrib(a, mu, sigma) / (sigma_eps + normal_distrib(a, mu_old, sigma_old))
            ratio2 = tf.exp(log_probs - log_probs_old)
            # Avoid division by 0 with small sigma_eps

            actor_loss = - tf.reduce_mean(tf.minimum(advantage * ratio,
                                                     advantage * tf.clip_by_value(ratio, 1 - epsilon, 1 + epsilon)))
            actor_loss2 = - tf.reduce_mean(tf.minimum(advantage2 * ratio2,
                                                      advantage2 * tf.clip_by_value(ratio2, 1 - epsilon, 1 + epsilon)))
        else:  # REINFORCE algorithm
            # actor_loss = - tf.reduce_mean(advantage * tf.math.log(normal_distrib(a, mu, sigma)))
            actor_loss = - tf.reduce_mean(advantage * log_probs)

        if insert_baseline:
            # loss2 = MSE(reward, b)  # Loss for the critic (Mean square error between return and the baseline)
            critic_loss = tf.reduce_mean(advantage ** 2)
            critic_loss2 = tf.reduce_mean(advantage2 ** 2)
            if concurrent_optimization:
                combined_loss = actor_loss + critic_loss_coeff * critic_loss
                combined_loss2 = actor_loss2 + critic_loss_coeff * critic_loss2

    # Compute gradients
    policy_grads = tape.gradient(actor_loss, [mu, sigma])
    if insert_baseline:
        value_grads = tape.gradient(critic_loss, b)
        if concurrent_optimization:
            combined_grads = tape.gradient(combined_loss, [mu, sigma, b])
            combined_grads = tf.clip_by_value(combined_grads, -grad_clip, grad_clip)
            combined_grads2 = tape.gradient(combined_loss2, [mu, sigma, b])
            combined_grads2 = tf.clip_by_value(combined_grads2, -grad_clip, grad_clip)
    # For PPO, update old parameters to have access to "old" policy
    if use_PPO:
        mu_old.assign(mu)
        sigma_old.assign(sigma)
        log_probs_old = log_probs

    data["amps"][i] = np.array(a)
    data["rewards"][i] = reward
    data["means"][i] = np.array(mu)
    data["stds"][i] = np.array(sigma)
    data["critic_loss"][i] = np.array(critic_loss)
    data["fidelity"][i] = qi.state_fidelity(target_state[tgt_string], dm_observed)

    # Apply gradients
    if insert_baseline:
        if concurrent_optimization:
            optimizer.apply_gradients(zip(combined_grads, tape.watched_variables()))
        else:
            optimizer_actor.apply_gradients(zip(policy_grads, (mu, sigma)))
            optimizer_critic.apply_gradients(zip([value_grads], [b]))
    else:
        optimizer.apply_gradients(zip(policy_grads, (mu, sigma)))
data["final_state"] = dm_observed
data["means"][-1] = np.array(mu)
data["stds"][-1] = np.array(sigma)

print(data)

if save_data:
    w = csv.writer(open(f"output_seed{seed}_lr{eta}.csv", "w"))

    # loop over dictionary keys and values
    for key, val in data.items():
        # write every key and value to file
        w.writerow([key, val])

"""
-----------------------------------------------------------------------------------------
-----------------------------------------------------------------------------------------
-----------------------------------------------------------------------------------------
Plotting tools
"""


#  Plotting results
def plot_examples(ax, reward_table):
    """
    Helper function to plot data with associated colormap, used for plotting the reward per each epoch and each episode
    (From original repo associated to the paper https://github.com/v-sivak/quantum-control-rl)
    """

    vals = np.where(reward_table == 1, 0.6, -0.9)

    ax.pcolormesh(np.transpose(vals), cmap='RdYlGn', vmin=-1, vmax=1)

    ax.set_xticks(np.arange(0, vals.shape[0], 1), minor=True)
    ax.set_yticks(np.arange(0, vals.shape[1], 1), minor=True)
    ax.grid(which='both', color='w', linestyle='-')
    ax.set_aspect('equal')
    ax.set_ylabel('Episode')
    ax.set_xlabel('Epoch')
    plt.show()


number_of_steps = 10
x = np.linspace(-1., 1., 100)
fig, (ax1, ax2, ax3) = plt.subplots(1, 3)
# Plot probability density associated to updated parameters for a few steps
for i in range(0, n_epochs + 1, number_of_steps):
    ax1.plot(x, norm.pdf(x, loc=data["means"][i], scale=np.abs(data["stds"][i])), '-o', label=f'{i}')

ax1.set_xlabel("Action, a")
ax1.set_ylabel("Probability density")
ax1.set_ylim(0., 20)
#  Plot return as a function of epochs
ax2.plot(np.mean(data["rewards"], axis=1), '-.', label='Reward')
ax2.set_xlabel("Epoch")
ax2.set_ylabel("Expected reward")
ax2.plot(data["critic_loss"], '-.', label='Critic Loss')
ax2.plot(data["fidelity"], '-o', label=f'State Fidelity (target: {tgt_string})')
ax2.legend()
ax1.legend()
plot_examples(ax3, data["rewards"])
