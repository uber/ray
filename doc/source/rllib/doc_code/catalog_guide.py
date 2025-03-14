# flake8: noqa
"""
This file holds several examples for the Catalogs API that are used in the catalog
guide.
"""


# 1) Basic interaction with Catalogs in RLlib.
# __sphinx_doc_basic_interaction_begin__
import gymnasium as gym

from ray.rllib.algorithms.ppo.ppo_catalog import PPOCatalog

env = gym.make("CartPole-v1")

catalog = PPOCatalog(env.observation_space, env.action_space, model_config_dict={})
# Build an encoder that fits CartPole's observation space.
encoder = catalog.build_actor_critic_encoder(framework="torch")
policy_head = catalog.build_pi_head(framework="torch")
# We expect a categorical distribution for CartPole.
action_dist_class = catalog.get_action_dist_cls(framework="torch")
# __sphinx_doc_basic_interaction_end__


# 2) Basic workflow that includes the Catalog base class and
# RLlib's ModelConfigs to build models and an action distribution to step through an
# environment.

# __sphinx_doc_modelsworkflow_begin__
import gymnasium as gym
import torch

from ray.rllib.core.models.base import STATE_IN, ENCODER_OUT
from ray.rllib.core.models.catalog import Catalog
from ray.rllib.core.models.configs import MLPHeadConfig
from ray.rllib.policy.sample_batch import SampleBatch

env = gym.make("CartPole-v1")

catalog = Catalog(env.observation_space, env.action_space, model_config_dict={})
# We expect a categorical distribution for CartPole.
action_dist_class = catalog.get_action_dist_cls(framework="torch")
# Therefore, we need `env.action_space.n` action distribution inputs.
expected_action_dist_input_dims = (env.action_space.n,)
# Build an encoder that fits CartPole's observation space.
encoder = catalog.build_encoder(framework="torch")
# Build a suitable head model for the action distribution.
head_config = MLPHeadConfig(
    input_dims=catalog.latent_dims, output_dims=expected_action_dist_input_dims
)
head = head_config.build(framework="torch")
# Now we are ready to interact with the environment
obs, info = env.reset()
# Encoders check for state and sequence lengths for recurrent models.
# We don't need either in this case because default encoders are not recurrent.
input_batch = {
    SampleBatch.OBS: torch.Tensor([obs]),
    STATE_IN: None,
    SampleBatch.SEQ_LENS: None,
}
# Pass the batch through our models and the action distribution.
encoding = encoder(input_batch)[ENCODER_OUT]
action_dist_inputs = head(encoding)
action_dist = action_dist_class.from_logits(action_dist_inputs)
actions = action_dist.sample().numpy()
env.step(actions[0])
# __sphinx_doc_modelsworkflow_end__


# 3) Demonstrates a basic workflow that includes the PPOCatalog to build models
# and an action distribution to step through an environment.

# __sphinx_doc_ppo_models_begin__
import gymnasium as gym
import torch

from ray.rllib.algorithms.ppo.ppo_catalog import PPOCatalog
from ray.rllib.core.models.base import STATE_IN, ENCODER_OUT, ACTOR
from ray.rllib.policy.sample_batch import SampleBatch

env = gym.make("CartPole-v1")

catalog = PPOCatalog(env.observation_space, env.action_space, model_config_dict={})
# Build an encoder that fits CartPole's observation space.
encoder = catalog.build_actor_critic_encoder(framework="torch")
policy_head = catalog.build_pi_head(framework="torch")
# We expect a categorical distribution for CartPole.
action_dist_class = catalog.get_action_dist_cls(framework="torch")

# Now we are ready to interact with the environment
obs, info = env.reset()
# Encoders check for state and sequence lengths for recurrent models.
# We don't need either in this case because default encoders are not recurrent.
input_batch = {
    SampleBatch.OBS: torch.Tensor([obs]),
    STATE_IN: None,
    SampleBatch.SEQ_LENS: None,
}
# Pass the batch through our models and the action distribution.
encoding = encoder(input_batch)[ENCODER_OUT][ACTOR]
action_dist_inputs = policy_head(encoding)
action_dist = action_dist_class.from_logits(action_dist_inputs)
actions = action_dist.sample().numpy()
env.step(actions[0])
# __sphinx_doc_ppo_models_end__


# 4) Demonstrates how to specify a Catalog for an RLModule to use through
# AlgorithmConfig.

# __sphinx_doc_algo_configs_begin__
from ray.rllib.algorithms.ppo.ppo_catalog import PPOCatalog
from ray.rllib.algorithms.ppo import PPOConfig
from ray.rllib.core.rl_module.rl_module import SingleAgentRLModuleSpec


class MyPPOCatalog(PPOCatalog):
    def __init__(self, *args, **kwargs):
        print("Hi from within PPORLModule!")
        super().__init__(*args, **kwargs)


config = (
    PPOConfig()
    .environment("CartPole-v1")
    .framework("torch")
    .rl_module(_enable_rl_module_api=True)
)

# Specify the catalog to use for the PPORLModule.
config = config.rl_module(
    rl_module_spec=SingleAgentRLModuleSpec(catalog_class=MyPPOCatalog)
)
# This is how RLlib constructs a PPORLModule
# It will say "Hi from within PPORLModule!".
ppo = config.build()
# __sphinx_doc_algo_configs_end__
