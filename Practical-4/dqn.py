import os
import random
import time

import gymnasium as gym
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

# Imports all our hyperparameters from the other file
from hyperparams import Hyperparameters as params

# stable_baselines3 have wrappers that simplifies 
# the preprocessing a lot, read more about them here:
# https://stable-baselines3.readthedocs.io/en/master/common/atari_wrappers.html
from stable_baselines3.common.atari_wrappers import (
    ClipRewardEnv,
    EpisodicLifeEnv,
    FireResetEnv,
    MaxAndSkipEnv,
    NoopResetEnv,
)
from stable_baselines3.common.buffers import ReplayBuffer

import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="gymnasium")

import logging
logging.getLogger("moviepy").setLevel(logging.CRITICAL)


# Creates our gym environment and with all our wrappers.
def make_env(env_id, seed, idx, capture_video, run_name):
    def thunk():
        env = gym.make(env_id, render_mode="rgb_array") 
        env = gym.wrappers.RecordEpisodeStatistics(env)
        if capture_video and idx == 0:
            env = gym.wrappers.RecordVideo(
                env,
                f"videos/{run_name}",
                episode_trigger=lambda step: step % 1000 == 0
            )
        env = NoopResetEnv(env, noop_max=30)
        env = MaxAndSkipEnv(env, skip=4)
        env = EpisodicLifeEnv(env)
        if "FIRE" in env.unwrapped.get_action_meanings():
            env = FireResetEnv(env)
        env = ClipRewardEnv(env)
        env = gym.wrappers.ResizeObservation(env, (84, 84))
        env = gym.wrappers.GrayScaleObservation(env)
        env = gym.wrappers.FrameStack(env, 4)
        env.reset(seed=seed)
        env.action_space.seed(seed)
        env.observation_space.seed(seed)
        return env
    return thunk


class QNetwork(nn.Module):
    def __init__(self, env):
        super().__init__()
        # TODO: Deinfe your network (agent)
        # Look at Section 4.1 in the paper for help: https://arxiv.org/pdf/1312.5602v1.pdf
        self.network = nn.Sequential(
            nn.Conv2d(4, 32, kernel_size=8, stride=4),
            nn.ReLU(),
            nn.Conv2d(32, 64, kernel_size=4, stride=2),
            nn.ReLU(),
            nn.Conv2d(64, 64, kernel_size=3, stride=1),
            nn.ReLU(),
            nn.Flatten(),
            nn.Linear(3136, 512),
            nn.ReLU(),
            nn.Linear(512, env.single_action_space.n),
        )

    def forward(self, x):
        return self.network(x / 255.0)


def linear_schedule(start_e: float, end_e: float, duration: int, t: int):
    slope = (end_e - start_e) / duration
    return max(slope * t + start_e, end_e)

if __name__ == "__main__":
    run_name = f"{params.env_id}__{params.exp_name}__{params.seed}__{int(time.time())}"

    random.seed(params.seed)
    np.random.seed(params.seed)
    torch.manual_seed(params.seed)
    torch.backends.cudnn.deterministic = params.torch_deterministic

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # env setup
    envs = gym.vector.SyncVectorEnv([make_env(params.env_id, params.seed, 0, params.capture_video, run_name)])
    assert isinstance(envs.single_action_space, gym.spaces.Discrete), "only discrete action space is supported"

    q_network = QNetwork(envs).to(device)
    optimizer = optim.Adam(q_network.parameters(), lr=params.learning_rate)
    target_network = QNetwork(envs).to(device)
    target_network.load_state_dict(q_network.state_dict())
    
    # We’ll be using experience replay memory for training our DQN. 
    # It stores the transitions that the agent observes, allowing us to reuse this data later. 
    # By sampling from it randomly, the transitions that build up a batch are decorrelated. 
    # It has been shown that this greatly stabilizes and improves the DQN training procedure.
    rb = ReplayBuffer(
        params.buffer_size,
        envs.single_observation_space,
        envs.single_action_space,
        device,
        optimize_memory_usage=False,
        handle_timeout_termination=True,
    )

    global_step = 0
    episode_counter = 0
    env_episode_counter = 0
    env_log_interval = 100
    num_life = 5
    episode_rewards = []
    start_time = time.time()

    obs, _ = envs.reset(seed=params.seed)

    try:
        for global_step in range(params.total_timesteps):
            # Here we get epsilon for our epislon greedy.
            epsilon = linear_schedule(params.start_e, params.end_e, params.exploration_fraction * params.total_timesteps, global_step)

            if random.random() < epsilon:
                # TODO: sample a random action from the environment 
                actions = np.array([envs.single_action_space.sample()])
            else:
                # TODO: get q_values from the network you defined, what should the network receive as input?
                q_values = q_network(torch.tensor(obs, device=device))
                actions = torch.argmax(q_values, dim=1).cpu().numpy()

            # Take a step in the environment
            next_obs, rewards, terminated, truncated, infos = envs.step(actions)
            dones = np.logical_or(terminated, truncated)

            # Save data to replay buffer
            real_next_obs = next_obs.copy()
            if "terminal_observation" in infos:
                for idx, done in enumerate(dones):
                    if done:
                        real_next_obs[idx] = infos["terminal_observation"][idx]

            # Here we store the transitions in D
            # Convert infos dict-of-lists to list-of-dicts (SB3 expects this format)
            infos_list = [{} for _ in range(len(dones))]  # empty info to avoid crash
            rb.add(obs, real_next_obs, actions, rewards, dones, infos_list)

            obs = next_obs
            global_step += 1

            # Save checkpoint periodically
            if global_step % 500_000 == 0:
                checkpoint_path = f"runs/{run_name}/{params.exp_name}_checkpoint_{global_step}.pt"
                os.makedirs(os.path.dirname(checkpoint_path), exist_ok=True)
                torch.save(q_network.state_dict(), checkpoint_path)
                print(f"Checkpoint saved at {checkpoint_path}")

            # Track episodic return
            if "final_info" in infos and infos["final_info"][0] is not None:
                episode_counter += 1

                # When enough real env episodes have completed, count as one logical episode
                if episode_counter % num_life == 0:
                    env_episode_counter += 1

                    if env_episode_counter % env_log_interval == 0:
                        final_info = infos["final_info"][0]
                        episode_return = final_info.get("episode_return", 0.0)
                        elapsed_minutes = (time.time() - start_time) / 60
                        log_line = (
                            f"Step: {global_step} | Episode {env_episode_counter} "
                            f"| Return: {episode_return} | Elapsed: {elapsed_minutes:.2f} min"
                        )
                        print(log_line, flush=True)
                        with open("training_log.txt", "a") as f:
                            f.write(log_line + "\n")

            # Training 
            if global_step > params.learning_starts:
                if global_step % params.train_frequency == 0:
                    # Sample random minibatch of transitions from D
                    data = rb.sample(params.batch_size)
                    # You can get data with:
                    # data.observation, data.rewards, data.dones, data.actions

                    with torch.no_grad():
                        # Now we calculate the y_j for non-terminal phi.
                        # TODO: Calculate max Q
                        # TODO: Calculate the td_target (y_j)
                        target_q = target_network(data.next_observations)
                        target_max = target_q.max(1)[0]
                        td_target = data.rewards.flatten() + params.gamma * target_max * (1 - data.dones.flatten())

                    current_q = q_network(data.observations).gather(1, data.actions).squeeze()
                    loss = F.mse_loss(current_q, td_target)

                    # perform our gradient decent step
                    optimizer.zero_grad()
                    loss.backward()
                    optimizer.step()

                # update target network
                if global_step % params.target_network_frequency == 0:
                    for target_network_param, q_network_param in zip(target_network.parameters(), q_network.parameters()):
                        target_network_param.data.copy_(
                            params.tau * q_network_param.data + (1.0 - params.tau) * target_network_param.data
                        )

    finally:
        if params.save_model:
            os.makedirs(f"runs/{run_name}", exist_ok=True)
            model_path = f"runs/{run_name}/{params.exp_name}_model"
            torch.save(q_network.state_dict(), model_path)
            print(f"\nModel saved to {model_path} at step {global_step}")
            
        envs.close()
