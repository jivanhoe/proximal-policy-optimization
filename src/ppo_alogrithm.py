from typing import Tuple, Union, Dict, List, Optional

import numpy as np
import logging
import torch
import torch.nn as nn
from copy import deepcopy
from gym.spaces import Box
from torch.distributions import Categorical
from utils.misc import concatenate_lists, timer


from robot_environments.pusher import PusherEnv
from robot_environments.reacher import ReacherEnv
from robot_environments.reacher_wall import ReacherWallEnv

# Set up device
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# Set up logging
logger = logging.getLogger(__name__)

# Constants
TOL = 1e-10


class ActorCritic(nn.Module):

    def __init__(
            self,
            state_space_dimension: int,
            action_space_dimension: int,
            action_map: Dict[int, np.array],
            actor_hidden_layer_units: List[int],
            critic_hidden_layer_units: List[int],
            non_linearity: nn.Module = nn.ReLU
    ):
        super(ActorCritic, self).__init__()

        # Make actor network
        actor_layers = [
            nn.Linear(state_space_dimension, actor_hidden_layer_units[0]),
            non_linearity()
        ]
        for i in range(1, len(actor_hidden_layer_units)):
            actor_layers += [
                nn.Linear(actor_hidden_layer_units[i - 1], actor_hidden_layer_units[i]),
                non_linearity()
            ]
        actor_layers += [
            nn.Linear(actor_hidden_layer_units[-1], action_space_dimension),
            nn.Softmax(dim=-1)
        ]
        self.actor = nn.Sequential(*actor_layers)

        # Make critic network
        critic_layers = [
            nn.Linear(state_space_dimension, critic_hidden_layer_units[0]),
            non_linearity()
        ]
        for i in range(1, len(critic_hidden_layer_units)):
            critic_layers += [
                nn.Linear(critic_hidden_layer_units[i - 1], critic_hidden_layer_units[i]),
                non_linearity()
            ]
        critic_layers += [
            nn.Linear(critic_hidden_layer_units[-1], 1)
        ]
        self.critic = nn.Sequential(*critic_layers)

        # Initialize other attributes
        self.action_map = action_map

    def forward(self, states: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        policy_probabilities = self.actor(states)
        values = self.critic(states)
        return policy_probabilities, values

    def sample_action(self, state: np.ndarray) -> np.ndarray:
        state = torch.tensor(state).float()
        action = Categorical(self.actor(state)).sample().item()
        return self.action_map[action]


class PPOLearner:

    def __init__(
            self,
            environment: Union[ReacherEnv, ReacherWallEnv, PusherEnv],
            state_space_dimension: int,
            action_space_dimension: int,
            action_map: Dict[int, np.array],
            critic_hidden_layer_units: List[int],
            actor_hidden_layer_units: List[int],
            random_init_box: Optional[Box] = None,
            n_steps_per_trajectory: int = 128,
            n_trajectories_per_batch: int = 8,
            n_epochs: int = 5,
            n_iterations: int = 50,
            learning_rate: float = 2.5e-4,
            discount: float = 0.99,
            clipping_param: float = 0.1,
            critic_coefficient: float = 1.0,
            entropy_coefficient: float = 0.1,
            entropy_decay: float = 0.999,
            reversion_threshold: float = 0.2,
            reversion_threshold_decay: float = 0.99,
            min_reversion_threshold: float = 0.1,
            seed: int = 0
    ):
        # Set seed
        self.seed = seed
        torch.manual_seed(seed)
        np.random.seed(seed)

        # Set environment attribute
        self.environment = environment

        # Initialize actor-critic policy network
        self.policy = ActorCritic(
            state_space_dimension=state_space_dimension,
            action_space_dimension=action_space_dimension,
            action_map=action_map,
            actor_hidden_layer_units=actor_hidden_layer_units,
            critic_hidden_layer_units=critic_hidden_layer_units,
        )

        # Initialize optimizer
        self.optimizer = torch.optim.Adam(self.policy.parameters(), lr=learning_rate)

        # Set hyperparameter attributes
        self.random_init_box = random_init_box
        self.n_steps_per_trajectory = n_steps_per_trajectory
        self.n_trajectories_per_batch = n_trajectories_per_batch
        self.n_epochs = n_epochs
        self.n_iterations = n_iterations
        self.discount = discount
        self.clipping_param = clipping_param
        self.critic_coefficient = critic_coefficient
        self.entropy_coefficient = entropy_coefficient
        self.entropy_decay = entropy_decay
        self.reversion_threshold = reversion_threshold
        self.reversion_threshold_decay = reversion_threshold_decay
        self.min_reversion_threshold = min_reversion_threshold
        if random_init_box is None:
            self.randomize_init_state = False
        else:
            self.randomize_init_state = True

        # Initialize attributes for performance tracking
        self.mean_rewards = []
        self.best_mean_reward = -np.inf
        self.best_policy = deepcopy(self.policy)

    def calculate_discounted_rewards(self, rewards: List[float]) -> List[float]:
        discounted_rewards = []
        discounted_reward = 0
        for t in reversed(range(self.n_steps_per_trajectory)):
            discounted_reward = rewards[t] + self.discount*discounted_reward
            discounted_rewards.insert(0, discounted_reward)
        return discounted_rewards

    def generate_trajectory(self) -> Tuple[List[np.ndarray], List[float], List[float]]:

        # Initialize trajectory
        states = []
        rewards = []

        # Set initial state
        init_state = self.environment.init
        if self.randomize_init_state:
            state = self.random_init_box.sample()
            self.environment.init = state
            self.environment.reset()
        else:
            state = init_state

        # Generate a sample trajectory under the current policy
        for _ in range(self.n_steps_per_trajectory + 1):

            # Sample from policy and receive feedback from environment
            action = self.policy.sample_action(state)
            new_state, reward, done, info = self.environment.step(action)

            # Store information from step
            states.append(state)
            rewards.append(reward)

            # Update state
            state = new_state

        # Reset environment initial state
        self.environment.init = init_state
        self.environment.reset()

        # Calculate discounted rewards
        discounted_rewards = self.calculate_discounted_rewards(rewards=rewards)

        # Return states (excluding terminal state), rewards and discounted rewards
        return states[:-1], rewards, discounted_rewards

    @timer
    def generate_batch(self) -> Tuple[List[np.ndarray], List[float], List[float]]:

        # Generate trajectories
        trajectories = [self.generate_trajectory() for _ in range(self.n_trajectories_per_batch)]

        # Unzip and return trajectories
        states, rewards, discounted_rewards = map(concatenate_lists, zip(*trajectories))
        return states, rewards, discounted_rewards

    def _track_performance(self, rewards):
        mean_reward = np.mean(rewards)
        self.mean_rewards.append(mean_reward)
        logger.info(f"Mean reward: {mean_reward}")
        if mean_reward > self.best_mean_reward:
            self.best_mean_reward = mean_reward
            self.best_policy = deepcopy(self.policy)
        elif (self.best_mean_reward - mean_reward) / self.best_mean_reward > self.reversion_threshold:
            self.policy = self.best_policy
            logger.info(f"Reverting policy!")

    def _get_tensors(
            self,
            states: List[np.ndarray],
            discounted_rewards: List[float],
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:

        # Convert data to tensors
        states = torch.tensor(states).float().to(device).detach()
        discounted_rewards = torch.tensor(discounted_rewards).float().unsqueeze(1).to(device).detach()
        old_policy_probabilities = self.policy.actor(states).float().to(device).detach()

        # Normalize discounted rewards
        discounted_rewards = (discounted_rewards - torch.mean(discounted_rewards)) / (torch.std(discounted_rewards) + TOL)

        return states, discounted_rewards, old_policy_probabilities

    def ppo_loss(
            self,
            values: torch.Tensor,
            discounted_rewards: torch.Tensor,
            policy_probabilities: torch.Tensor,
            old_policy_probabilities: torch.Tensor,
            advantage_estimates: torch.Tensor,
    ) -> torch.Tensor:
        ratio = torch.exp(torch.log(policy_probabilities + TOL) - torch.log(old_policy_probabilities + TOL))
        actor_loss = -torch.mean(
            torch.min(
                torch.clamp(ratio, 1 - self.clipping_param, 1 + self.clipping_param) * advantage_estimates,
                ratio * advantage_estimates
            )
        )
        critic_loss = torch.mean((discounted_rewards - values).pow(2))
        entropy_loss = torch.mean(policy_probabilities * torch.log(policy_probabilities + TOL))
        return actor_loss + self.critic_coefficient * critic_loss + self.entropy_coefficient * entropy_loss

    @timer
    def update_policy_network(
            self,
            states: torch.Tensor,
            discounted_rewards: torch.Tensor,
            old_policy_probabilities: torch.Tensor
    ) -> None:

        for _ in range(self.n_epochs):

            # Get policy network outputs
            policy_probabilities, values = self.policy(states)

            # Get advantage estimates
            advantage_estimates = (discounted_rewards - values.detach())

            # Calculate loss
            loss = self.ppo_loss(
                values=values,
                discounted_rewards=discounted_rewards,
                policy_probabilities=policy_probabilities,
                old_policy_probabilities=old_policy_probabilities,
                advantage_estimates=advantage_estimates
            )

            # Perform gradient update
            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()

    def update_hyperparameters(self) -> None:
        # Decay entropy coefficient
        self.entropy_coefficient = self.entropy_decay * self.entropy_coefficient

        # Decay reversion threshold
        self.reversion_threshold = max(self.reversion_threshold_decay * self.reversion_threshold_decay,
                                       self.min_reversion_threshold)

    def train(self) -> None:

        for i in range(self.n_iterations):

            logger.info(f"Iteration: {i + 1}")

            # Generate batch
            states, rewards, discounted_rewards = self.generate_batch()

            # Track performance
            self._track_performance(rewards=rewards)

            # Convert data to PyTorch tensors
            states, discounted_rewards, old_policy_probabilities = self._get_tensors(
                states=states,
                discounted_rewards=discounted_rewards
            )

            # Perform gradient updates
            self.update_policy_network(
               states=states,
               discounted_rewards=discounted_rewards,
               old_policy_probabilities=old_policy_probabilities
            )

            # Update parameters
            self.update_hyperparameters()

            logger.info("-" * 50)





