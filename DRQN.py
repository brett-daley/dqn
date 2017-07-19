import sys
sys.path.append("nn_archs/")
import time
import tensorflow as tf 
import numpy as np 
import random
from collections import deque 
import os
from LineplotDynamic import LineplotDynamic
from utils_general import ReplayMemory
from utils_general import TfSaver
from AgentDistilled import AgentDistilled

# Hyper Parameters:
GAMMA = 0.95 # decay rate of past observations
EPSILON_INIT = 1.0 # Starting value of epsilon (for exploration)
EPSILON_FINAL = 0.1 # Final value of epsilon (for exploration)
TARGET_Q_UPDATE_FREQ = 1000
EPS_TEST_TIME = 0.01

class DRQN:
	def __init__(self, cfg_parser, n_actions, sess, dim_obs_agts=None, agt=None):
		self.cfg_parser = cfg_parser		
		self.sess = sess
		
		self.n_iter_pretrain = int(self.cfg_parser.get('root','n_iter_pretrain')) # Initial phase where no training occurs. Allows population of replay memory before training.		
		self.n_iter_explore = int(self.cfg_parser.get('root','n_iter_explore')) # Random policy exploration phase - frames over which to anneal epsilon
		self.double_q_learning = self.cfg_parser.getboolean('root','double_q_learning')
		self.minibatch_size = int(self.cfg_parser.get('root','minibatch_size')) # Size of replay memory transition minibatch in each training phase
		
		# init some parameters
		self.epsilon = EPSILON_INIT
		self.n_actions = n_actions

		self.agt = agt

		if self.agt.nn.is_rnn:
			self.tracelength = int(self.cfg_parser.get('root','rnn_train_tracelength'))
		else:
			self.tracelength = 1

		# init replay memory (n_trajs_max is the number of trajectories! Each trajectory has many samples within it!)
		self.replay_memory = ReplayMemory(n_trajs_max=200, minibatch_size=self.minibatch_size)

		# Init plotting
		self.plot_qvalue_dqn = LineplotDynamic('Training Epoch','Q','')
		self.plot_init_qvalue_dqn = LineplotDynamic('Training Epoch','Value anticipated','')
		self.plot_value_dqn = LineplotDynamic('Training Epoch','Value actual','')

	def init_mdrqn_agts(self):
		self.agt = AgentDistilled(i_agt=0, n_actions=self.n_actions, dim_obs=self.dim_obs_agts[0])

		# Initialize agent NNs (need the game initialized at this point, since will need things like observation dimensions well-defined)
		self.create_mdrqn_nns()

	def reset_agts_rnn_states(self):
		self.agt.reset_rnn_state()

	def create_mdrqn_nns(self):
		# Create actual player NNs
		self.parameter_sharing = False # each agent has its own distilled net for now
		self.agt.create_nns(cfg_parser=self.cfg_parser, sess=self.sess, scope_suffix=scope_suffix, parameter_sharing=self.parameter_sharing)

	def init_agts_nnTs(self):
		self.agt.init_nnT()

	def update_Q_plot(self, timestep, hl_name=None, label=None, s_batch_prespecified=None, a_batch_prespecified=None):
		agt = self.agt

		if s_batch_prespecified is None:
			# Since tracelength of 1 used, this ensures that truetracelengths = [1,....,1] and no masking required below
			_, minibatch = self.replay_memory.sample_trace(tracelength = 1)
			s_batch = np.vstack([row[0] for row in minibatch[:,0]])
			a_batch = np.vstack([row[0] for row in minibatch[:,1]])
			minibatch_size_plot = self.minibatch_size
		else:
			s_batch = s_batch_prespecified
			# a_batch = a_batch_prespecified
			# QValue = self.get_qvalue(agt = agt, input_obs = input_obs)
			minibatch_size_plot = len(s_batch_prespecified)

		x = timestep
		# TODO is the best thing being plotted here? Q_action is the q value for a specific action in the replay history, whereas i think we want to plot the q value of the BEST action chosen based on whatever the NN tells us (should be similar to the get_action function)
		# y = agt.nn.Q_Action.eval(feed_dict={agt.nn.actionInput : a_batch,
		# 									agt.nn.stateInput : s_batch,
		# 									agt.nn.tracelength: 1,
		# 									agt.nn.truetracelengths: [1]*minibatch_size_plot,
		# 									agt.nn.batch_size: minibatch_size_plot })

		y = agt.nn.Qmax.eval(feed_dict={agt.nn.stateInput : s_batch,
										agt.nn.tracelength: 1,
										agt.nn.truetracelengths: [1]*minibatch_size_plot,
										agt.nn.batch_size: minibatch_size_plot })
		

		y_mean = np.mean(y)
		y_stdev = np.std(y)

		if hl_name == None or label ==None:
			if s_batch_prespecified is None:
				self.plot_qvalue_dqn.update(hl_name = 'q_plot', x_new = x, y_new = y_mean, y_stdev_new = y_stdev)
			else:
				self.plot_init_qvalue_dqn.update(hl_name = 'q_plot', x_new = x, y_new = y_mean, y_stdev_new = y_stdev)
		else:
			if s_batch_prespecified is None:
				self.plot_qvalue_dqn.update(hl_name = hl_name, label = label, x_new = x, y_new = y_mean, y_stdev_new = y_stdev)
			else:
				self.plot_init_qvalue_dqn.update(hl_name = hl_name, label = label, x_new = x, y_new = y_mean, y_stdev_new = y_stdev)

		return x, y_mean, y_stdev

	def get_processed_minibatch(self):
			if self.agt.nn.is_rnn:
				truetracelengths, minibatch = self.replay_memory.sample_trace(tracelength = self.tracelength)
			else:
				truetracelengths, minibatch = self.replay_memory.sample_trace(tracelength = 1)

			# Rewards and termination signals are shared by all agents, so only process once
			r_batch = minibatch[:,2]
			non_terminal_multiplier = -(minibatch[:,4] - 1)

			return minibatch, truetracelengths, r_batch, non_terminal_multiplier

	def train_Q_network(self, timestep):
		if (timestep > self.n_iter_pretrain):
			# Step 1: sample random minibatch of transitions from replay memory
			minibatch, truetracelengths, r_batch, non_terminal_multiplier = self.get_processed_minibatch()

			agt = self.agt
			s_batch = np.vstack([row[agt.i] for row in minibatch[:,0]])
			a_batch = np.vstack([row[agt.i] for row in minibatch[:,1]])
			s_next_batch = np.vstack([row[agt.i] for row in minibatch[:,3]])
			# if np.count_nonzero(r_batch):
			# 	print '-----'
			# 	print "r_batch ",r_batch
			# 	print "non_terminal_multiplier",non_terminal_multiplier

			# Calculate DRQN target
			feed_dict = {agt.nnT.stateInput : s_next_batch,
						 agt.nnT.tracelength: self.tracelength,
						 agt.nnT.truetracelengths: truetracelengths,
						 agt.nnT.batch_size: self.minibatch_size}

			if agt.nnT.is_rnn:
				rnn_state_train = (np.zeros([self.minibatch_size,agt.nn.h_size]),np.zeros([self.minibatch_size,agt.nn.h_size])) 
				feed_dict[agt.nnT.rnn_state_in] = rnn_state_train

			if self.double_q_learning:
				#Below we perform the Double-DQN update to the target Q-values
				Q2 = self.sess.run(agt.nnT.QValue, feed_dict=feed_dict)

				# Also need to compose a feed_dict for agt.nn
				feed_dict = {agt.nn.stateInput : s_next_batch,
							 agt.nn.tracelength: self.tracelength,
							 agt.nn.truetracelengths: truetracelengths,
							 agt.nn.batch_size: self.minibatch_size}
				if agt.nn.is_rnn:
					feed_dict[agt.nn.rnn_state_in] = rnn_state_train
				predict_nn_actions = self.sess.run(agt.nn.predict, feed_dict=feed_dict)

				doubleQ = Q2[range(self.minibatch_size*self.tracelength),predict_nn_actions]
				y_batch = r_batch + (GAMMA*doubleQ * non_terminal_multiplier)

			else:
				QmaxT = agt.nnT.Qmax.eval(feed_dict=feed_dict)
				y_batch = r_batch + (GAMMA*QmaxT * non_terminal_multiplier)

			# Train
			feed_dict={agt.nn.yInput : y_batch,
						agt.nn.actionInput : a_batch,
						agt.nn.stateInput : s_batch,
						agt.nn.tracelength: self.tracelength,
						agt.nn.truetracelengths: truetracelengths,
						agt.nn.batch_size: self.minibatch_size}

			# Q = agt.nn.Q_Action.eval(feed_dict = feed_dict)
			# print Q

			if agt.nn.is_rnn:
				feed_dict[agt.nn.rnn_state_in] = rnn_state_train

			# seqlen_mask = self.sess.run(agt.nn.seqlen_mask, feed_dict=feed_dict)
			# print truetracelengths
			# print seqlen_mask

			# print agt.nn.td_err.eval(feed_dict = feed_dict)
			agt.nn.trainStep.run(feed_dict = feed_dict)

			# Delay in a target network update - to improve learning stability
			# TODO 2017 I believe target copy needs to happen just once in parameter sharing case, after final agent's update only
			if timestep % TARGET_Q_UPDATE_FREQ == 0:
				assert agt.nnT != None
				agt.nnT.run_copy()
		
		self.log_training_phase(timestep)

	def log_training_phase(self, timestep):
		if timestep % 100 == 0:
			if timestep <= self.n_iter_pretrain:
				state = "pre-train"
			elif timestep > self.n_iter_pretrain and timestep <= self.n_iter_pretrain + self.n_iter_explore:
				state = "train (e-greedy)"
			else:
				state = "train (e-greedy, min epsilon reached)"
			print "ITER", timestep, "| PHASE", state, "| EPSILON", self.epsilon

	def dec_epsilon(self, timestep):
		# Linearly decrease epsilon
		if self.epsilon > EPSILON_FINAL and timestep > self.n_iter_pretrain:
			self.epsilon -= (EPSILON_INIT - EPSILON_FINAL)/self.n_iter_explore

	def get_qvalue(self, agt, input_obs):
		feed_dict= {agt.nn.stateInput:[input_obs],
					 agt.nn.tracelength: 1,
					 agt.nn.truetracelengths: [1],
					 agt.nn.batch_size: 1}

		if agt.nn.is_rnn:
			feed_dict[agt.nn.rnn_state_in] = agt.rnn_state
			QValue, agt.rnn_state = self.sess.run([agt.nn.QValue, agt.nn.rnn_state], feed_dict= feed_dict)
		else:
			QValue = self.sess.run(agt.nn.QValue, feed_dict = feed_dict)
			agt.rnn_state = None

		# print agt.i, agt.rnn_state

		# stateInput is usually a batch input (due to mini-batch training), so [0] at end just grabs the QValue for this single input case
		return QValue[0]

	def get_action(self, agt, timestep, input_obs, test_mode=False, epsilon=None):
		if epsilon is not None:
			epsilon_to_use = epsilon
		else:
			epsilon_to_use = self.epsilon

		# print epsilon_to_use

		QValue = self.get_qvalue(agt = agt, input_obs = input_obs)

		action_onehot = np.zeros(agt.n_actions)
		action_index = 0

		# Select e-greedy action (also during pre-training phase)
		if (not test_mode and random.random() <= epsilon_to_use) or (not test_mode and timestep < self.n_iter_pretrain) or (test_mode and random.random() <= EPS_TEST_TIME):
			action_index = random.randrange(agt.n_actions)
			action_onehot[action_index] = 1
		# Select optimal action
		else:
			action_index = np.argmax(QValue)
			action_onehot[action_index] = 1

			# if i_game == 1:
			# 	print 'obs', input_obs
			# 	print 'QValue', QValue
		return action_onehot, QValue