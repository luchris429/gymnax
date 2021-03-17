import jax
import jax.numpy as jnp
from jax import jit

# JAX Compatible version of Asterix MinAtar environment. Source:
# github.com/kenjyoung/MinAtar/blob/master/minatar/environments/asterix.py

# Default environment parameters of Asterix game
params_asterix = {
                  "ramping": 1,
                  "ramp_interval": 100,
                  "init_spawn_speed": 10,
                  "init_move_interval": 5,
                  "shot_cool_down": 5,
                 }

"""
- Player moves freely along 4 cardinal dirs.
- Enemies and treasure spawn from the sides.
- A reward of +1 is given for picking up treasure.
- Termination occurs if the player makes contact with an enemy.
- Enemy and treasure direction are indicated by a trail channel.
- Difficulty periodically increases: the speed/spawn rate of enemies/treasure.
- Channels are encoded as follows: 'player':0, 'enemy':1, 'trail':2, 'gold':3
- Observation has dimensionality (10, 10, 4)
- Actions are encoded as follows: 'l': 0, 'u': 1, 'r': 2, 'd': 3.
- Note that is different from MinAtar where 0 is 'n' and 5 is 'f' (None/Fire).
"""


def step(rng_input, params, state, action):
    """ Perform single timestep state transition. """
    # Spawn enemy if timer is up - sample at each step and mask
    entity, slot = spawn_entity(rng_input, state)
    state["entities"] = ((state["spawn_timer"] == 0) *
                          jax.ops.index_update(state["entities"],
                                               jax.ops.index[slot], entity)
                          + (state["spawn_timer"] > 0) * state["entities"])
    state["spawn_timer"] = ((state["spawn_timer"] == 0) * state["spawn_speed"]
                            + (state["spawn_timer"] > 0) * state["spawn_timer"])

    # Resolve player action via implicit conditional updates of coordinates
    player_x = (jnp.maximum(0, state["player_x"] - 1) * (action == 0)  # l
                + jnp.minimum(9, state["player_x"] + 1) * (action == 2)  # r
                + state["player_x"] * jnp.logical_and(action != 0,
                                                      action != 2))  # others

    player_y = (jnp.maximum(1, state["player_y"] - 1) * (action == 1)  # u
                + jnp.minimum(8, state["player_y"] + 1) * (action == 3)  # d
                + state["player_y"] * jnp.logical_and(action != 1,
                                                      action != 3))  # others

    state["player_x"] = player_x
    state["player_y"] = player_y

    # Update entities, get reward and figure out termination
    state, reward, done = update_entities(state)

    # Update various timers
    state["spawn_timer"] -= 1
    state["move_timer"] -= 1

    # Ramp difficulty if interval has elapsed
    ramp_cond = jnp.logical_and(params["ramping"],
                                jnp.logical_or(state["spawn_speed"] > 1,
                                               state["move_speed"] > 1))
    # 1. Update ramp_timer
    timer_cond = jnp.logical_and(ramp_cond, state["ramp_timer"] >= 0)
    state["ramp_timer"] = (timer_cond * (state["ramp_timer"] - 1)
                           + (1 - timer_cond) * params["ramp_interval"])
    # 2. Update move_speed
    move_speed_cond = jnp.logical_and(
                jnp.logical_and(ramp_cond, 1 - timer_cond),
                jnp.logical_and(state["move_speed"], state["ramp_index"] % 2))
    state["move_speed"] -= move_speed_cond
    # 3. Update spawn_speed
    spawn_speed_cond = jnp.logical_and(
                            jnp.logical_and(ramp_cond, 1 - timer_cond),
                            state["spawn_speed"] > 1)
    state["spawn_speed"] -= spawn_speed_cond
    # 4. Update ramp_index
    state["ramp_index"] += jnp.logical_and(ramp_cond, 1 - timer_cond)
    return get_obs(state), state, reward, done, {}


def reset(rng_input, params):
    """ Reset environment state by reseting state to fixed position. """
    state = {
        "player_x": 5,
        "player_y": 5,
        "shot_timer": 0,
        "spawn_speed": params["init_spawn_speed"],
        "spawn_timer": params["init_spawn_speed"],
        "move_speed": params["init_move_interval"],
        "move_timer": params["init_move_interval"],
        "ramp_timer": params["ramp_interval"],
        "ramp_index": 0,
        "entities": jnp.zeros((8, 5), dtype=int)
    }
    return get_obs(state), state


def get_obs(state):
    """ Return observation from raw state trafo. """
    # Add a 5th channel to help with not used entities
    obs = jnp.zeros((10, 10, 5), dtype=bool)
    # Set the position of the agent in the grid
    obs = jax.ops.index_update(obs, jax.ops.index[state["player_y"],
                                                  state["player_x"],
                                                  0], 1)
    # Loop over entity identities and set entity locations
    # TODO: Rewrite as scan?! Not too important? Only 8 entities
    for i in range(state["entities"].shape[0]):
        x = state["entities"][i, :]
        # Enemy channel 1, Trail channel 2, Gold channel 3, Not used 4
        c = 3 * x[3] + 1 * (1 - x[3])
        c_eff = c * x[4] + 4 * (1 - x[4])
        obs = jax.ops.index_update(obs, jax.ops.index[x[1], x[0], c_eff], 1)

        back_x = (x[0] - 1) * x[2] + (x[0] + 1) * (1 - x[2])
        leave_trail = jnp.logical_and(back_x >= 0, back_x<=9)
        c_eff = 2 * x[4] + 4 * (1 - x[4])
        obs = jax.ops.index_update(obs, jax.ops.index[x[1], back_x, c_eff],
                                   leave_trail)
    return obs[:, :, :4]


def spawn_entity(rng_input, state):
    """ Spawn new enemy or treasure at random location
        with random direction (if all rows are filled do nothing).
    """
    key_lr, key_gold, key_slot = jax.random.split(rng_input, 3)
    lr = jax.random.choice(key_lr, jnp.array([1, 0]))
    is_gold = jax.random.choice(key_gold, jnp.array([1, 0]),
                                p=jnp.array([1/3, 2/3]))
    x = (1 - lr) * 9
    # Entities are represented as 5 dimensional arrays
    # 0: Position y, 1: Slot x, 2: lr, 3: Gold indicator
    # 4: whether entity is filled/not an open slot

    # Sampling problem: Need to get rid of jnp.where due to concretization
    # Sample random order of entries to go through
    # Check if element is free with while loop and stop if position is found
    # or all elements have been checked
    state_entities = state["entities"][:, 4]
    slot, free = while_sample_slots(rng_input, state_entities)
    entity = jnp.array([x, slot+1, lr, is_gold, free])
    return entity, slot


def while_sample_slots(rng_input, state_entities):
    """ Go through random order of slots until slot is found that is free. """
    init_val = jnp.array([0, 0])
    # Sample random order of slot entries to go through - hack around jnp.where
    order_to_go_through = jax.random.permutation(rng_input, jnp.arange(8))
    def condition_to_check(val):
        # Check if we haven't gone through all possible slots and whether free
        return jnp.logical_and(val[0] < 7, val[1] == 0)
    def update(val):
        # Increase list counter - slot that has been checked
        val = jax.ops.index_update(val, 0, val[0] + 1)
        # Check if slot is still free
        free = (state_entities[val[0]] == 0)
        val = jax.ops.index_update(val, 1, free)
        return val
    id_and_free = jax.lax.while_loop(condition_to_check, update, init_val)
    # Return slot id and whether it is free
    return id_and_free[0], id_and_free[1]


def update_entities(state):
    """ Update positions of the entities and return reward, done. """
    done, reward = False, 0
    # Loop over entities and check for collisions - either gold or enemy
    for i in range(8):
        x = state["entities"][i]
        slot_filled = (x[4] != 0)
        collision = jnp.logical_and(
                        x[0:2] == [state["player_x"], state["player_y"]],
                        slot_filled)
        # If collision with gold: empty gold and give positive reward
        collision_gold = jnp.logical_and(collision, x[3])
        reward += collision_gold
        state["entities"] = jax.ops.index_update(state["entities"], i,
                                                 x * (1 - collision_gold))
        # If collision with enemy: terminate the episode
        collision_enemy = jnp.logical_and(collision, 1 - x[3])
        done = collision_enemy

    # Loop over entities and move them in direction
    time_to_move = (state["move_timer"] == 0)
    state["move_timer"] = (time_to_move * state["move_speed"]
                           + (1 - time_to_move) * state["move_speed"])
    for i in range(8):
        x = state["entities"][i]
        slot_filled = (x[4] != 0)
        lr = x[2]
        # Update position left and right move
        x = jax.ops.index_update(x, 0,
                                 (slot_filled * (x[0] + 1 * lr - 1 * (1 - lr))
                                 + (1 - slot_filled) * x[0]))

        # Update if entity moves out of the frame - reset everything to zeros
        outside_of_frame = jnp.logical_or(x[0] < 0, x[0] > 9)
        state["entities"] = jax.ops.index_update(state["entities"], i,
                                                 x * slot_filled
                                                 * (1 - outside_of_frame))

        # Update if entity moves into the player after its state is updated
        collision = jnp.logical_and(
                        x[0:2] == [state["player_x"], state["player_y"]],
                        slot_filled)
        # If collision with gold: empty gold and give positive reward
        collision_gold = jnp.logical_and(collision, x[3])
        reward += collision_gold
        state["entities"] = jax.ops.index_update(state["entities"], i,
                                                 x * (1 - collision_gold))
        # If collision with enemy: terminate the episode
        collision_enemy = jnp.logical_and(collision, 1 - x[3])
        done = collision_enemy
    return state, reward, done


reset_asterix = jit(reset)
step_asterix = jit(step)
