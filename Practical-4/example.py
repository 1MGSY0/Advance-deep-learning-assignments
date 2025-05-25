# If you are running this on a server you will not be able to use render_mode="human"
# instead use render_mode="rgb_array" since you cannot ouput to a display.

# WARNING: This may not run on M1 macs.
# If you really want to run the example, I recommend creating a seperate
# conda env and installing gym with pip install gym[atari, accept-rom-license] 
# But use the ex6 environment when working on the dqn file.

import gymnasium as gym

env = gym.make("ALE/Breakout-v5", render_mode="human") 
obs, info = env.reset(seed=42)

for _ in range(1000):
    action = env.action_space.sample()
    obs, reward, terminated, truncated, info = env.step(action)
    done = terminated or truncated
    if done:
        obs, info = env.reset()
env.close()
print("Executed environment successfully.")
