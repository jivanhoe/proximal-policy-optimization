import logging

import numpy as np
import torch
from torch.utils.data import TensorDataset

from algorithms.ppo import PPOLearner
from algorithms.behavior_cloning import BCLearner
from environment_models.pusher import PusherEnv
from architectures.actor_critic import ActorCritic

logger = logging.basicConfig(level=logging.DEBUG)

# Set paths
EXPERT_DATA_PATH = "../../data/expert.npz"
RESULTS_FOLDER = "../../data/results/pusher/"

if __name__ == "__main__":

    # Load data
    expert_data = np.load(EXPERT_DATA_PATH)
    expert_data = TensorDataset(torch.tensor(expert_data["obs"]), torch.tensor(expert_data["action"]))

    environment = PusherEnv()

    policy = ActorCritic(
        state_space_dimension=environment.state_space_dimension,
        action_space_dimension=environment.action_space_dimension,
        actor_hidden_layer_units=(128, 64),
        critic_hidden_layer_units=(64, 32),
        actor_std=5e-3
    )

    bc_learner = BCLearner(
        policy=policy,
        n_epochs=50,
        batch_size=128,
        learning_rate=3e-4
    )
    bc_learner.train(expert_data=expert_data)
    bc_learner.policy.save(path=f"{RESULTS_FOLDER}bc_model.pt")

    ppo_with_bc_learner = PPOLearner(
        environment=environment,
        policy=bc_learner.policy,
        n_steps_per_trajectory=16,
        n_trajectories_per_batch=64,
        n_epochs=5,
        n_iterations=20,
        learning_rate=3e-4,
        clipping_param=0.2,
        entropy_coefficient=1e-3,
        bc_coefficient=1e-3
    )
    ppo_with_bc_learner.train(expert_data=expert_data, train_critic_only_on_init=True)
    ppo_with_bc_learner.policy.save(path=f"{RESULTS_FOLDER}ppo_with_bc_model.pt")



