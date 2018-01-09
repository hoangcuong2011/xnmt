from __future__ import division, generators

from subprocess import Popen
import random
import io
import six

import numpy as np
import dynet as dy

from xnmt.serializer import Serializable, YamlSerializer, DependentInitParam
from xnmt.loss import LossBuilder
from xnmt.inference import SimpleInference
from xnmt.events import register_xnmt_event
from xnmt.loss_calculator import LossCalculator, MLELoss
from xnmt.batcher import SrcBatcher
from xnmt.loss_tracker import BatchLossTracker
import xnmt.xnmt_evaluate
from xnmt.evaluator import LossScore

class TrainingTask(object):
  """
  Base class for a training task. Training tasks can perform training steps
  and keep track of the training state, but may not implement the actual training
  loop.
  """
  def __init__(self, model):
    self.model = model
  
  def should_stop_training(self):
    """
    :returns: True iff training is finished, i.e. training_step(...) should not be called again
    """
    raise NotImplementedError("")
  
  def training_step(self, src, trg):
    """
    Performs forward pass corresponding to a single training step.
    Training logic like switching epochs, reshuffling batches, etc. must be
    handled as well.
    
    :param src: src minibatch
    :param trg: trg minibatch
    :returns: Loss
    """
    raise NotImplementedError("")

  def checkpoint_needed(self):
    raise NotImplementedError()

  def checkpoint(self, control_learning_schedule=False, out_ext=".dev_hyp", ref_ext=".dev_ref", 
                 encoding='utf-8'):
    """
    Performs a dev checkpoint
    :param control_learning_schedule: If False, only evaluate dev data.
                                      If True, also perform model saving, LR decay etc. if needed.
    :param out_ext:
    :param ref_ext:
    :param encoding:
    :returns: True if the model needs saving, False otherwise
    """
    raise NotImplementedError()


class SimpleTrainingTask(TrainingTask, Serializable):
  yaml_tag = u'!SimpleTrainingTask'
  def __init__(self, yaml_context, model, glob={},
               src_file=None, trg_file=None,
               dev_every=0, batcher=None, loss_calculator=None, 
               pretrained_model_file="", src_format="text", run_for_epochs=None,
               lr_decay=1.0, lr_decay_times=3, patience=1, initial_patience=None,
               dev_tasks=None, restart_trainer=False,
               reload_command=None, name=None, inference=None):
    """
    :param yaml_context:
    :param model: a generator.GeneratorModel object
    :param glob: global settings
    :param src_file: The file for the source data.
    :param trg_file: The file for the target data.
    :param dev_every (int): dev checkpoints every n sentences (0 for only after epoch)
    :param batcher: Type of batcher. Defaults to SrcBatcher of batch size 32.
    :param loss_calculator:
    :param pretrained_model_file: Path of pre-trained model file
    :param src_format: Format of input data: text/contvec
    :param lr_decay (float):
    :param lr_decay_times (int):  Early stopping after decaying learning rate a certain number of times
    :param patience (int): apply LR decay after dev scores haven't improved over this many checkpoints
    :param initial_patience (int): if given, allows adjusting patience for the first LR decay
    :param dev_tasks: A list of tasks to run on the development set
    :param restart_trainer: Restart trainer (useful for Adam) and revert weights to best dev checkpoint when applying LR decay (https://arxiv.org/pdf/1706.09733.pdf)
    :param reload_command: Command to change the input data after each epoch.
                           --epoch EPOCH_NUM will be appended to the command.
                           To just reload the data after each epoch set the command to 'true'.
    :param name: will be prepended to log outputs if given
    :param inference: used for inference during dev checkpoints if dev_metrics are specified
    """
    assert yaml_context is not None
    self.yaml_context = yaml_context
    self.model_file = self.yaml_context.dynet_param_collection.model_file
    self.yaml_serializer = YamlSerializer()
    self.src_file = src_file
    self.trg_file = trg_file
    self.dev_tasks = dev_tasks

    if lr_decay > 1.0 or lr_decay <= 0.0:
      raise RuntimeError("illegal lr_decay, must satisfy: 0.0 < lr_decay <= 1.0")
    self.lr_decay = lr_decay
    self.patience = patience
    self.initial_patience = initial_patience
    self.lr_decay_times = lr_decay_times
    self.restart_trainer = restart_trainer
    self.run_for_epochs = run_for_epochs
    
    self.early_stopping_reached = False
    # training state
    self.training_state = TrainingState()

    self.reload_command = reload_command
    if reload_command is not None:
        self._augmentation_handle = None
        self._augment_data_initial()

    self.model = model
    self.loss_calculator = loss_calculator or LossCalculator(MLELoss())
    self.pretrained_model_file = pretrained_model_file
    if self.pretrained_model_file:
      self.yaml_context.dynet_param_collection.load_from_data_file(self.pretrained_model_file + '.data')

    # TODO: self.sample_train_sents and self.max_num_train_sents should be initialized properly
    self.sample_train_sents = False
    self.max_num_train_sents = None
    # TODO: I'm not sure whether these should be kept around or removed
    self.max_src_len = None
    self.max_trg_len = None

    self.batcher = batcher or SrcBatcher(32)
    if src_format == "contvec":
      self.batcher.pad_token = np.zeros(self.model.src_embedder.emb_dim)
    self.read_training_corpus()
    self.logger = BatchLossTracker(self, dev_every, name)
  
  def read_training_corpus(self):
    self.src_data = []
    self.trg_data = []
    src_len = self.model.src_reader.count_sents(self.src_file)
    trg_len = self.model.trg_reader.count_sents(self.trg_file)
    if self.sample_train_sents:
      if src_len != trg_len: raise RuntimeError("training src sentences don't match trg sentences: %s != %s!" % (src_len, trg_len))
      self.sample_train_sents = int(self.sample_train_sents)
      filter_ids = np.random.choice(src_len, self.sample_train_sents, replace=False)
    elif self.max_num_train_sents:
      if src_len != trg_len: raise RuntimeError("training src sentences don't match trg sentences: %s != %s!" % (src_len, trg_len))
      filter_ids = list(range(min(self.max_num_train_sents, trg_len)))
    else:
      filter_ids = None
    src_train_iterator = self.model.src_reader.read_sents(self.src_file, filter_ids)
    trg_train_iterator = self.model.trg_reader.read_sents(self.trg_file, filter_ids)
    for src_sent, trg_sent in six.moves.zip_longest(src_train_iterator, trg_train_iterator):
      if src_sent is None or trg_sent is None:
        raise RuntimeError("training src sentences don't match trg sentences: %s != %s!" % (src_len or self.src_reader.count_sents(self.src_file), trg_len or self.trg_reader.count_sents(self.trg_file)))
      src_len_ok = self.max_src_len is None or len(src_sent) <= self.max_src_len
      trg_len_ok = self.max_trg_len is None or len(trg_sent) <= self.max_trg_len
      if src_len_ok and trg_len_ok:
        self.src_data.append(src_sent)
        self.trg_data.append(trg_sent)

    # TODO: Should we actually be doing this here?
    self.model.src_reader.freeze()
    self.model.trg_reader.freeze()

    # Pack batches
    self.src_batches, self.trg_batches = \
      self.batcher.pack(self.src_data, self.trg_data)

  def _augment_data_initial(self):
    """
    Called before loading corpus for the first time, if reload_command is given
    """
    augment_command = self.reload_command
    print('initial augmentation')
    if self._augmentation_handle is None:
      # first run
      self._augmentation_handle = Popen(augment_command + " --epoch 0", shell=True)
      self._augmentation_handle.wait()

  def _augment_data_next_epoch(self):
    """
    This is run in the background if reload_command is given to prepare data for the next epoch
    """
    augment_command = self.reload_command
    if self._augmentation_handle is None:
      # first run
      self._augmentation_handle = Popen(augment_command + " --epoch %d" % self.training_state.epoch_num, shell=True)
      self._augmentation_handle.wait()
   
    self._augmentation_handle.poll()
    retcode = self._augmentation_handle.returncode
    if retcode is not None:
      if self.training_state.epoch_num > 0:
        print('using reloaded data')
      # reload the data   
      self.corpus_parser._read_training_corpus(self.corpus_parser.training_corpus) # TODO: fix
      # restart data generation
      self._augmentation_handle = Popen(augment_command + " --epoch %d" % self.training_state.epoch_num, shell=True)
    else:
      print('new data set is not ready yet, using data from last epoch.')

  @register_xnmt_event
  def new_epoch(self, training_regimen, num_sents):
    """
    New epoch event.
    :param training_regimen: Indicates which training regimen is advancing to the next epoch.
    :param num_sents: Number of sentences in the upcoming epoch (may change between epochs)
    """
    pass

  def should_stop_training(self):
    """
    Signal stopping if self.early_stopping_reached is marked or we exhausted the number of requested epochs.
    """
    return self.early_stopping_reached \
      or self.training_state.epoch_num > self.run_for_epochs \
      or (self.training_state.epoch_num == self.run_for_epochs and self.training_state.steps_into_epoch >= self.cur_num_minibatches()-1)
  
  def cur_num_minibatches(self):
    """
    Current number of minibatches (may change between epochs, e.g. for randomizing batchers or if reload_command is given)
    """
    return len(self.src_batches)
  
  def cur_num_sentences(self):
    """
    Current number of parallel sentences (may change between epochs, e.g. if reload_command is given)
    """
    return len(self.src_data)
  
  def advance_epoch(self):
    """
    Shifts internal state to the next epoch, including batch re-packing and shuffling.
    """
    if self.reload_command is not None:
      self._augment_data_next_epoch()
    self.training_state.epoch_seed = random.randint(1,2147483647)
    random.seed(self.training_state.epoch_seed)
    np.random.seed(self.training_state.epoch_seed)
    self.src_batches, self.trg_batches = \
      self.batcher.pack(self.src_data, self.trg_data)
    self.training_state.epoch_num += 1
    self.training_state.steps_into_epoch = 0
    self.minibatch_order = list(range(0, self.cur_num_minibatches()))
    np.random.shuffle(self.minibatch_order)
    self.new_epoch(training_regimen=self, num_sents=self.cur_num_sentences())
  
  def next_minibatch(self):
    """
    Infinitely loops over training minibatches and calls advance_epoch() after every complete sweep over the corpus.
    :returns: Generator yielding (src_batch,trg_batch) tuples 
    """
    while True:
      self.advance_epoch()
      for batch_num in self.minibatch_order:
        src = self.src_batches[batch_num]
        trg = self.trg_batches[batch_num]
        yield src, trg
        self.training_state.steps_into_epoch += 1
  
  def training_step(self, src, trg):
    """
    Performs forward pass, backward pass, parameter update for the given minibatch
    """
    loss_builder = LossBuilder()
    standard_loss = self.model.calc_loss(src, trg, self.loss_calculator)
    if standard_loss.__class__ == LossBuilder:
      loss = None
      for loss_name, loss_expr in standard_loss.loss_nodes:
        loss_builder.add_loss(loss_name, loss_expr)
        loss = loss_expr if not loss else loss + loss_expr
      standard_loss = loss

    else:
      loss_builder.add_loss("loss", standard_loss)

    additional_loss = self.model.calc_additional_loss(dy.nobackprop(-standard_loss))
    if additional_loss != None:
      loss_builder.add_loss("additional_loss", additional_loss)

    loss_value = loss_builder.compute()
    self.logger.update_epoch_loss(src, trg, loss_builder)
    self.logger.report_train_process()

    return loss_value
    
  def checkpoint_needed(self):
    return self.logger.should_report_dev()

  def checkpoint(self, control_learning_schedule=True):
    """
    Performs a dev checkpoint
    :param control_learning_schedule: If False, only evaluate dev data.
                                      If True, also perform model saving, LR decay etc. if needed.
    :returns: True if the model needs saving, False otherwise
    """
    ret = False
    self.logger.new_dev()

    # Perform evaluation
    if self.dev_tasks and len(self.dev_tasks) > 0:
      dev_scores = []
      for dev_task in self.dev_tasks:
        dev_score = dev_task.eval()
        if type(dev_score) == list:
          dev_scores.extend(dev_score)
        else:
          dev_scores.append(dev_score)
      # TODO: This is passing "1" for the number of words, as this is not implemented yet
      self.logger.set_dev_score(1, dev_scores[0])
      for dev_score in dev_scores[1:]:
        self.logger.report_auxiliary_score(dev_score)
    
    # Control the learning schedule
    if control_learning_schedule:
      print("> Checkpoint")
      # Write out the model if it's the best one
      if self.logger.report_dev_and_check_model(self.model_file):
        if self.model_file is not None:
          ret = True
        self.training_state.cur_attempt = 0
      else:
        # otherwise: learning rate decay / early stopping
        self.training_state.cur_attempt += 1
        if self.lr_decay < 1.0:
          should_decay = False
          if (self.initial_patience is None or self.training_state.num_times_lr_decayed>0) and self.training_state.cur_attempt >= self.patience:
            should_decay = True
          if self.initial_patience is not None and self.training_state.num_times_lr_decayed==0 and self.training_state.cur_attempt >= self.initial_patience:
            should_decay = True
          if should_decay:
            self.training_state.num_times_lr_decayed += 1
            if self.training_state.num_times_lr_decayed > self.lr_decay_times:
              print('  Early stopping')
              self.early_stopping_reached = True
            else:
              self.trainer.learning_rate *= self.lr_decay
              print('  new learning rate: %s' % self.trainer.learning_rate)
              if self.restart_trainer:
                print('  restarting trainer and reverting learned weights to best checkpoint..')
                self.trainer.restart()
                self.yaml_context.dynet_param_collection.revert_to_best_model()

    return ret

class TrainingState(object):
  """
  This holds the state of the training loop.
  """
  def __init__(self):
    self.num_times_lr_decayed = 0
    self.cur_attempt = 0
    self.epoch_num = 0
    self.steps_into_epoch = 0
    # used to pack and shuffle minibatches; storing helps resuming crashed trainings
    self.epoch_seed = random.randint(1,2147483647)
