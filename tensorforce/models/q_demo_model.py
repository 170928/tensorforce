# Copyright 2017 reinforce.io. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
import tensorflow as tf

from tensorforce import util, TensorForceError
from tensorforce.core.memories import Memory
from tensorforce.models import QModel


class QDemoModel(QModel):
    """
    Model for deep Q-learning from demonstration. Principal structure similar to double
    deep Q-networks but uses additional loss terms for demo data.
    """

    def __init__(
        self,
        states_spec,
        actions_spec,
        device,
        session_config,
        scope,
        saver_spec,
        summary_spec,
        distributed_spec,
        variable_noise,
        states_preprocessing,
        actions_exploration,
        reward_preprocessing,
        memory,
        update_spec,
        optimizer,
        discount,
        network,
        distributions,
        entropy_regularization,
        target_sync_frequency,
        target_update_weight,
        double_q_model,
        huber_loss,
        expert_margin,
        supervised_weight,
        demo_memory_capacity,
        demo_batch_size
    ):
        if any(action['type'] not in ('bool', 'int') for action in actions_spec.values()):
            raise TensorForceError("Invalid action type, only 'bool' and 'int' are valid!")

        self.expert_margin = expert_margin
        self.supervised_weight = supervised_weight
        self.demo_batch_size = demo_batch_size
        self.demo_memory_spec = memory
        super(QDemoModel, self).__init__(
            states_spec=states_spec,
            actions_spec=actions_spec,
            device=device,
            session_config=session_config,
            scope=scope,
            saver_spec=saver_spec,
            summary_spec=summary_spec,
            distributed_spec=distributed_spec,
            variable_noise=variable_noise,
            states_preprocessing=states_preprocessing,
            actions_exploration=actions_exploration,
            reward_preprocessing=reward_preprocessing,
            memory=memory,
            update_spec=update_spec,
            optimizer=optimizer,
            discount=discount,
            network=network,
            distributions=distributions,
            entropy_regularization=entropy_regularization,
            target_sync_frequency=target_sync_frequency,
            target_update_weight=target_update_weight,
            double_q_model=double_q_model,
            huber_loss=huber_loss
        )

    def initialize(self, custom_getter):
        super(QDemoModel, self).initialize(custom_getter=custom_getter)
        self.demo_memory = Memory.from_spec(
            spec=self.demo_memory_spec,
            kwargs=dict(
                states_spec=self.states_spec,
                actions_spec=self.actions_spec,
                include_next_states=False, # TODO why is this not configurable?
                summary_labels=self.summary_labels
            )
        )
        self.demo_memory.initialize()

        # Demonstration loss
        self.fn_demo_loss = tf.make_template(
            name_='demo-loss',
            func_=self.tf_demo_loss,
            custom_getter_=custom_getter
        )

        # Demonstration optimization
        self.fn_demo_optimization = tf.make_template(
            name_='demo-optimization',
            func_=self.tf_demo_optimization,
            custom_getter_=custom_getter
        )

    def tf_optimization(self, states, internals, actions, terminal, reward, next_states=None, next_internals=None):
        optimization = super(QDemoModel, self).tf_optimization(
            states=states,
            internals=internals,
            actions=actions,
            reward=reward,
            terminal=terminal,
            next_states=next_states,
            next_internals=next_internals
        )

        # TODO args mismatch
        self.demo_optimization = self.fn_demo_optimization(
            states=states,
            internals=internals,
            actions=actions,
            reward=reward,
            terminal=terminal,
            next_states=next_states,
            next_internals=next_internals
        )

        return tf.group(optimization, self.demo_optimization)

    def tf_demo_loss(self, states, actions, terminal, reward, internals, update):
        embedding = self.network.apply(x=states, internals=internals, update=update)
        deltas = list()

        for name, distribution in self.distributions.items():
            distr_params = distribution.parameters(x=embedding)
            state_action_values = distribution.state_action_values(distr_params=distr_params)

            # Create the supervised margin loss
            # Zero for the action taken, one for all other actions, now multiply by expert margin
            if self.actions_spec[name]['type'] == 'bool':
                num_actions = 2
            else:
                num_actions = self.actions_spec[name]['num_actions']
            one_hot = tf.one_hot(indices=actions[name], depth=num_actions)
            ones = tf.ones_like(tensor=one_hot, dtype=tf.float32)
            inverted_one_hot = ones - one_hot

            # max_a([Q(s,a) + l(s,a_E,a)], l(s,a_E, a) is 0 for expert action and margin value for others
            expert_margin = distr_params + inverted_one_hot * self.expert_margin

            # J_E(Q) = max_a([Q(s,a) + l(s,a_E,a)] - Q(s,a_E)
            supervised_selector = tf.reduce_max(input_tensor=expert_margin, axis=-1)
            delta = supervised_selector - state_action_values
            delta = tf.reshape(tensor=delta, shape=(-1, util.prod(self.actions_spec[name]['shape'])))
            deltas.append(delta)

        loss_per_instance = tf.reduce_mean(input_tensor=tf.concat(values=deltas, axis=1), axis=1)
        loss_per_instance = tf.square(x=loss_per_instance)
        return tf.reduce_mean(input_tensor=loss_per_instance, axis=0)

    # TODO api mismatch
    def tf_demo_optimization(self, states, internals, actions, terminal, reward, update):

        def fn_loss():
            # Combining q-loss with demonstration loss
            q_model_loss = self.fn_loss(
                states=states,
                internals=internals,
                actions=actions,
                terminal=terminal,
                reward=reward,
                update=update
            )
            demo_loss = self.fn_demo_loss(
                states=states,
                internals=internals,
                actions=actions,
                terminal=terminal,
                reward=reward,
                update=update
            )
            return q_model_loss + self.supervised_weight * demo_loss

        demo_optimization = self.optimizer.minimize(
            time=self.timestep,
            variables=self.get_variables(),
            fn_loss=fn_loss
        )

        target_optimization = self.target_optimizer.minimize(
            time=self.timestep,
            variables=self.target_network.get_variables(),
            source_variables=self.network.get_variables()
        )

        return tf.group(demo_optimization, target_optimization)

    def set_demo_memory(self, states, internals, actions, terminal, reward):
        """
        Stores demonstrations in the demo memory.
        """
        # TODO check if this is correct
        feed_dict = dict(
            states=states,
            internals=internals,
            actions=actions,
            terminal=terminal,
            reward=reward
        )
        fetches = self.demo_memory.store

        self.monitored_session.run(fetches=fetches, feed_dict=feed_dict)

    def demonstration_update(self):
        # TODO args are now fetched from internal demo memory
        fetches = self.demo_optimization

        # terminal = np.asarray(terminal)
        # batched = (terminal.ndim == 1)
        # if batched:
        #     # TEMP: Random sampling fix
        #     if self.random_sampling_fix:
        #         feed_dict = {state_input: states[name][0] for name, state_input in self.states_input.items()}
        #         feed_dict.update(
        #             {state_input: states[name][1] for name, state_input in self.next_states_input.items()}
        #         )
        #     else:
        #         feed_dict = {state_input: states[name] for name, state_input in self.states_input.items()}
        #     feed_dict.update(
        #         {internal_input: internals[n]
        #             for n, internal_input in enumerate(self.internals_input)}
        #     )
        #     feed_dict.update(
        #         {action_input: actions[name]
        #             for name, action_input in self.actions_input.items()}
        #     )
        #     feed_dict[self.terminal_input] = terminal
        #     feed_dict[self.reward_input] = reward
        # else:
        #     # TEMP: Random sampling fix
        #     if self.random_sampling_fix:
        #         raise TensorForceError("Unbatched version not covered by fix.")
        #     else:
        #         feed_dict = {state_input: (states[name],) for name, state_input in self.states_input.items()}
        #     feed_dict.update(
        #         {internal_input: (internals[n],)
        #             for n, internal_input in enumerate(self.internals_input)}
        #     )
        #     feed_dict.update(
        #         {action_input: (actions[name],)
        #             for name, action_input in self.actions_input.items()}
        #     )
        #     feed_dict[self.terminal_input] = (terminal,)
        #     feed_dict[self.reward_input] = (reward,)
        #
        # feed_dict[self.deterministic_input] = True
        # feed_dict[self.update_input] = True
        #
        # self.monitored_session.run(fetches=fetches, feed_dict=feed_dict)
