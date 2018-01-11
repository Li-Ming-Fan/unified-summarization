# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
# Modifications Copyright 2017 Abigail See
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

"""This is the top-level file to train, evaluate or test your summarization model"""

import sys
import time
import os
import tensorflow as tf
import numpy as np
from collections import namedtuple
from selector.model import SentenceSelector
from selector.evaluate import SelectorEvaluator
import selector.run_selector as run_selector
from rewriter.model import Rewriter
from rewriter.decode import BeamSearchDecoder
import rewriter.run_rewriter as run_rewriter
from data import Vocab
from batcher import Batcher
import run_end2end
import evaluate_end2end
import util
import pdb

FLAGS = tf.app.flags.FLAGS

# Where to find data
tf.app.flags.DEFINE_string('data_path', '', 'Path expression to tf.Example datafiles. Can include wildcards to access multiple datafiles.')
tf.app.flags.DEFINE_string('vocab_path', '', 'Path expression to text vocabulary file.')

# Important settings
tf.app.flags.DEFINE_string('model', '', 'must be one of selector/rewriter/end2end')
tf.app.flags.DEFINE_string('mode', 'train', 'must be one of train/eval/evalall')
tf.app.flags.DEFINE_boolean('single_pass', False, 'For decode mode only. If True, run eval on the full dataset using a fixed checkpoint, i.e. take the current checkpoint, and use it to produce one summary for each example in the dataset, write the summaries to file and then get ROUGE scores for the whole dataset. If False (default), run concurrent decoding, i.e. repeatedly load latest checkpoint, use it to produce summaries for randomly-chosen examples and log the results to screen, indefinitely.')

# Where to save output
tf.app.flags.DEFINE_integer('max_train_iter', 29000, 'max iterations to train')
tf.app.flags.DEFINE_integer('save_model_every', 10, 'save the model every N iterations')
tf.app.flags.DEFINE_integer('model_max_to_keep', 3, 'save latest N models')
tf.app.flags.DEFINE_string('log_root', '', 'Root directory for all logging.')
tf.app.flags.DEFINE_string('exp_name', '', 'Name for experiment. Logs will be saved in a directory with this name, under log_root.')
tf.app.flags.DEFINE_boolean('load_best_val_model', False, '')
tf.app.flags.DEFINE_boolean('load_best_test_model', False, '')
tf.app.flags.DEFINE_string('eval_ckpt_path', '', 'checkpoint path for evalall mode')

# Hyperparameters for both selector and rewriter
tf.app.flags.DEFINE_integer('batch_size', 16, 'minibatch size')
tf.app.flags.DEFINE_integer('emb_dim', 128, 'dimension of word embeddings')
tf.app.flags.DEFINE_integer('vocab_size', 50000, 'Size of vocabulary. These will be read from the vocabulary file in order. If the vocabulary file contains fewer words than this number, or if this number is set to 0, will take all words in the vocabulary file.')
tf.app.flags.DEFINE_float('lr', 0.15, 'learning rate')
tf.app.flags.DEFINE_float('adagrad_init_acc', 0.1, 'initial accumulator value for Adagrad')
tf.app.flags.DEFINE_float('rand_unif_init_mag', 0.02, 'magnitude for lstm cells random uniform inititalization')
tf.app.flags.DEFINE_float('trunc_norm_init_std', 1e-4, 'std of trunc norm init, used for initializing everything else')
tf.app.flags.DEFINE_float('max_grad_norm', 2.0, 'for gradient clipping')

# Hyperparameters for selector only
tf.app.flags.DEFINE_string('loss', 'CE', 'CE/FL (cross entropy/focal loss)')
tf.app.flags.DEFINE_float('gamma', 2.0, 'gamma used only in focal loss')
tf.app.flags.DEFINE_string('rnn_type', 'GRU', 'LSTM/GRU')
tf.app.flags.DEFINE_integer('hidden_dim_selector', 200, 'dimension of RNN hidden states')
tf.app.flags.DEFINE_integer('max_art_len', 100, 'max timesteps of sentence-level encoder')
tf.app.flags.DEFINE_integer('max_sent_len', 50, 'max timesteps of word-level encoder')
tf.app.flags.DEFINE_float('thres', 0.4, 'threshold of probabilities')
tf.app.flags.DEFINE_integer('min_select_sent', 5, 'min sentences need to be selected')
tf.app.flags.DEFINE_integer('max_select_sent', 20, 'max sentences to be selected')
tf.app.flags.DEFINE_boolean('eval_gt_rouge', False, 'whether to evaluate ground-truth selected sentences ROUGE scores')
tf.app.flags.DEFINE_boolean('eval_rouge', False, 'whether to evaluate ROUGE scores')
tf.app.flags.DEFINE_boolean('save_pkl', False, 'whether to save the results as pickle files')
tf.app.flags.DEFINE_boolean('save_bin', True, 'whether to save the results as binary files')
tf.app.flags.DEFINE_boolean('plot', True, 'whether to plot the precision/recall and recall/ratio curves')

# Hyperparameters for rewriter only
tf.app.flags.DEFINE_integer('hidden_dim_rewriter', 256, 'dimension of RNN hidden states')
tf.app.flags.DEFINE_integer('max_enc_steps', 400, 'max timesteps of encoder (max source text tokens)')
tf.app.flags.DEFINE_integer('max_dec_steps', 100, 'max timesteps of decoder (max summary tokens)')
tf.app.flags.DEFINE_integer('beam_size', 4, 'beam size for beam search decoding.')
tf.app.flags.DEFINE_integer('min_dec_steps', 35, 'Minimum sequence length of generated summary. Applies only for beam search decoding mode')

# Pointer-generator or baseline model
tf.app.flags.DEFINE_boolean('pointer_gen', True, 'If True, use pointer-generator model. If False, use baseline model.')

# Coverage hyperparameters
tf.app.flags.DEFINE_boolean('coverage', False, 'Use coverage mechanism. Note, the experiments reported in the ACL paper train WITHOUT coverage until converged, and then train for a short phase WITH coverage afterwards. i.e. to reproduce the results in the ACL paper, turn this off for most of training then turn on for a short phase at the end.')
tf.app.flags.DEFINE_float('cov_loss_wt', 1.0, 'Weight of coverage loss (lambda in the paper). If zero, then no incentive to minimize coverage loss.')
tf.app.flags.DEFINE_boolean('convert_to_coverage_model', False, 'Convert a non-coverage model to a coverage model. Turn this on and run in train mode. Your current model will be copied to a new version (same name with _cov_init appended) that will be ready to run with coverage flag turned on, for the coverage training stage.')

tf.app.flags.DEFINE_boolean('convert_to_pointer_model', False, 'Convert a non-pointer model to a pointer model.')


def main(unused_argv):
  if len(unused_argv) != 1: # prints a message if you've entered flags incorrectly
    raise Exception("Problem with flags: %s" % unused_argv)

  tf.logging.set_verbosity(tf.logging.INFO) # choose what level of logging you want
  if FLAGS.model not in ['selector', 'rewriter', 'end2end']:
    raise ValueError("The 'model' flag must be one of selector/rewriter/end2end")
  if FLAGS.mode not in ['train', 'eval', 'evalall']:
    raise ValueError("The 'mode' flag must be one of train/eval/evalall")
  tf.logging.info('Starting %s in %s mode...' % (FLAGS.model, FLAGS.mode))

  # Change log_root to FLAGS.log_root/FLAGS.exp_name and create the dir if necessary
  FLAGS.log_root = os.path.join(FLAGS.log_root, FLAGS.model, FLAGS.exp_name)
  if not os.path.exists(FLAGS.log_root):
    if FLAGS.mode=="train":
      os.makedirs(FLAGS.log_root)
    else:
      raise Exception("Logdir %s doesn't exist. Run in train mode to create it." % (FLAGS.log_root))

  vocab = Vocab(FLAGS.vocab_path, FLAGS.vocab_size) # create a vocabulary

  # If in evalall mode, set batch_size = 1 or beam_size
  # Reason: in evalall mode, we decode one example at a time.
  # For rewriter, on each step, we have beam_size-many hypotheses in the beam, 
  # so we need to make a batch of these hypotheses.
  if FLAGS.mode == 'evalall':
    if FLAGS.model == 'selector':
      FLAGS.batch_size = 1
    else:
      FLAGS.batch_size = FLAGS.beam_size

  # If single_pass=True, check we're in evalall mode
  if FLAGS.single_pass and FLAGS.mode!='evalall':
    raise Exception("The single_pass flag should only be True in evalall mode")

  # Make a namedtuple hps, containing the values of the hyperparameters that the model needs
  hparam_list = ['model', 'mode', 'rnn_type', 'loss', 'gamma', 'lr', 'adagrad_init_acc', 'rand_unif_init_mag', 'trunc_norm_init_std', 'max_grad_norm', 'hidden_dim_selector', 'hidden_dim_rewriter','emb_dim', 'batch_size', 'max_art_len', 'max_sent_len', 'max_dec_steps', 'max_enc_steps', 'coverage', 'cov_loss_wt', 'pointer_gen', 'eval_gt_rouge']
  hps_dict = {}
  for key,val in FLAGS.__flags.iteritems(): # for each flag
    if key in hparam_list: # if it's in the list
      hps_dict[key] = val # add it to the dict
  hps = namedtuple("HParams", hps_dict.keys())(**hps_dict)

  # Create a batcher object that will create minibatches of data
  batcher = Batcher(FLAGS.data_path, vocab, hps, single_pass=FLAGS.single_pass)

  tf.set_random_seed(111) # a seed value for randomness

  if FLAGS.model == 'selector':
    if hps.mode == 'train':
      print "creating model..."
      model = SentenceSelector(hps, vocab)
      run_selector.setup_training(model, batcher)
    elif hps.mode == 'eval':
      model = SentenceSelector(hps, vocab)
      run_selector.run_eval(model, batcher)
    elif hps.mode == 'evalall':
      model = SentenceSelector(hps, vocab)
      evaluator = SelectorEvaluator(model, batcher, vocab)
      evaluator.evaluate()
  elif FLAGS.model == 'rewriter':
    if hps.mode == 'train':
      print "creating model..."
      model = Rewriter(hps, vocab)
      run_rewriter.setup_training(model, batcher)
    elif hps.mode == 'eval':
      model = Rewriter(hps, vocab)
      run_rewriter.run_eval(model, batcher)
    elif hps.mode == 'evalall':
      decode_model_hps = hps  # This will be the hyperparameters for the decoder model
      decode_model_hps = hps._replace(max_dec_steps=1) # The model is configured with max_dec_steps=1 because we only ever run one step of the decoder at a time (to do beam search). Note that the batcher is initialized with max_dec_steps equal to e.g. 100 because the batches need to contain the full summaries
      model = Rewriter(decode_model_hps, vocab)
      decoder = BeamSearchDecoder(model, batcher, vocab)
      decoder.decode() # decode indefinitely (unless single_pass=True, in which case deocde the dataset exactly once)
  elif FLAGS.model == 'end2end':
    if hps.mode == 'train':
      print "creating model..."
      select_model = SentenceSelector(hps, vocab)
      rewrite_model = Rewriter(hps, vocab)
      run_end2end.setup_training(select_model, rewrite_model, batcher)
    elif hps.mode == 'eval':
      select_model = SentenceSelector(hps, vocab)
      rewrite_model = Rewriter(hps, vocab)
      run_end2end.run_eval(select_model, rewrite_model, batcher)
    elif hps.mode == 'evalall':
      eval_model_hps = hps  # This will be the hyperparameters for the decoder model
      eval_model_hps = hps._replace(max_dec_steps=1) # The model is configured with max_dec_steps=1 because we only ever run one step of the decoder at a time (to do beam search). Note that the batcher is initialized with max_dec_steps equal to e.g. 100 because the batches need to contain the full summaries
      select_model = selector.model.SentenceSelector(eval_model_hps, vocab)
      rewrite_model = rewriter.model.Rewriter(eval_model_hps, vocab)
      evaluator = evaluate_end2end.End2EndEvaluator(model, batcher, vocab)
      evaluator.evaluate() # decode indefinitely (unless single_pass=True, in which case deocde the dataset exactly once)

if __name__ == '__main__':
  tf.app.run()
