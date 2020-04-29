from typing import Callable, Tuple, Optional

import numpy as np


class BaseEnv:

    def __init__(
            self,
            initial_state,
            transition_function: Callable,
            reward_function: Callable,
            is_done: Optional[Callable] = None,
            state_space_dimension: Optional[int] = None,
            action_space_dimension: Optional[int] = None,
    ):
        self.initial_state = initial_state
        self.state = initial_state
        self.transition_function = transition_function
        self.reward_function = reward_function
        self.is_done = is_done if is_done else lambda state: False
        self.state_space_dimension = state_space_dimension if state_space_dimension else len(self.state)
        self.action_space_dimension = action_space_dimension if action_space_dimension else len(self.state)

    def update(self, action) -> Tuple[float, bool]:
        self.state = self.transition_function(state=self.state, action=action)
        done = self.is_done(self.state)
        reward = self.reward_function(state=self.state, action=action)
        return reward, done

    def set_state(self, state) -> None:
        self.state = state

    def reset(self) -> None:
        self.state = self.initial_state



