#  Copyright (c) 2019  PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"
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

import os
import collections
import paddle.fluid as fluid
import time
import numpy as np
import multiprocessing

from paddle_hub.tools.logger import logger
from paddle_hub.finetune.optimization import bert_optimization
from paddle_hub.finetune.config import FinetuneConfig

__all__ = ['append_mlp_classifier']


def append_mlp_classifier(feature, label, num_classes=2, hidden_units=None):
    cls_feats = fluid.layers.dropout(
        x=feature, dropout_prob=0.1, dropout_implementation="upscale_in_train")

    # append fully connected layer according to hidden_units
    if hidden_units != None:
        for n_hidden in hidden_units:
            cls_feats = fluid.layers.fc(input=cls_feats, size=n_hidden)

    logits = fluid.layers.fc(
        input=cls_feats,
        size=num_classes,
        param_attr=fluid.ParamAttr(
            name="cls_out_w",
            initializer=fluid.initializer.TruncatedNormal(scale=0.02)),
        bias_attr=fluid.ParamAttr(
            name="cls_out_b", initializer=fluid.initializer.Constant(0.)))

    ce_loss, probs = fluid.layers.softmax_with_cross_entropy(
        logits=logits, label=label, return_softmax=True)
    loss = fluid.layers.mean(x=ce_loss)

    num_example = fluid.layers.create_tensor(dtype='int64')
    accuracy = fluid.layers.accuracy(
        input=probs, label=label, total=num_example)

    # TODO: encapsulate to Task
    graph_var_dict = {
        "loss": loss,
        "probs": probs,
        "accuracy": accuracy,
        "num_example": num_example
    }

    task = Task("text_classification", graph_var_dict,
                fluid.default_main_program(), fluid.default_startup_program())

    return task


def finetune_and_eval(task, feed_list, data_processor, config=None):
    if config.use_cuda:
        place = fluid.CUDAPlace(int(os.getenv('FLAGS_selected_gpus', '0')))
        dev_count = fluid.core.get_cuda_device_count()
    else:
        place = fluid.CPUPlace()
        dev_count = int(os.environ.get('CPU_NUM', multiprocessing.cpu_count()))

    # data generator
    data_generator = {
        'train':
        data_processor.data_generator(
            batch_size=config.batch_size,
            phase='train',
            epoch=config.epoch,
            shuffle=False),
        'test':
        data_processor.data_generator(
            batch_size=config.batch_size, phase='test', shuffle=False),
        'dev':
        data_processor.data_generator(
            batch_size=config.batch_size, phase='dev', shuffle=False)
    }

    # hub.finetune_and_eval start here
    #TODO: to simplify
    loss = task.variable("loss")
    probs = task.variable("probs")
    accuracy = task.variable("accuracy")
    num_example = task.variable("num_example")

    num_train_examples = data_processor.get_num_examples(phase='train')
    if config.in_tokens:
        max_train_steps = config.epoch * num_train_examples // (
            config.batch_size // config.max_seq_len) // dev_count
    else:
        max_train_steps = config.epoch * num_train_examples // config.batch_size // dev_count

    warmup_steps = int(max_train_steps * config.warmup_proportion)

    # obtain main program from Task class
    train_program = task.main_program()
    startup_program = task.startup_program()
    # clone test program before optimize
    test_program = train_program.clone(for_test=True)

    bert_optimization(loss, warmup_steps, max_train_steps, config.learning_rate,
                      train_program, config.weight_decay)

    # memory optimization
    fluid.memory_optimize(
        input_program=train_program,
        skip_opt_set=[
            # skip task graph variable memory optimization
            loss.name,
            probs.name,
            accuracy.name,
            num_example.name
        ])

    place = fluid.CUDAPlace(0)
    exe = fluid.Executor(place)
    exe.run(startup_program)
    feeder = fluid.DataFeeder(feed_list=feed_list, place=place)

    # Traning block
    # prepare training dataset
    train_data_generator = data_generator['train']
    total_loss, total_acc, total_num_example = [], [], []
    step = 0
    time_begin = time.time()
    train_time_used = 0.0
    for example in train_data_generator():
        step += 1
        train_time_begin = time.time()
        np_loss, np_acc, np_num_example = exe.run(
            program=train_program,
            feed=feeder.feed([example]),
            fetch_list=[loss, accuracy, num_example])
        train_time_used += time.time() - train_time_begin

        # Statistic Block
        total_loss.extend(np_loss * np_num_example)
        total_acc.extend(np_acc * np_num_example)
        total_num_example.extend(np_num_example)
        if step % config.stat_interval == 0:
            # get training progress
            accum_num_example = np.sum(total_num_example)
            print("step {}: loss={:.5f} acc={:.5f} [step/sec: {:.2f}]".format(
                step,
                np.sum(total_loss) / accum_num_example,
                np.sum(total_acc) / accum_num_example,
                config.stat_interval / train_time_used))
            # reset statistic variables
            total_loss, total_acc, total_num_example = [], [], []
            train_time_used = 0.0

        # Evaluation block
        if step % config.eval_interval == 0:
            print("Evaluation start")
            total_loss, total_acc, total_num_example = [], [], []
            dev_data_generator = data_generator['dev']

            eval_step = 0
            eval_time_begin = time.time()
            for example in dev_data_generator():
                eval_step += 1
                np_loss, np_acc, np_num_example = exe.run(
                    program=test_program,
                    feed=feeder.feed([example]),
                    fetch_list=[loss, accuracy, num_example])
                total_loss.extend(np_loss * np_num_example)
                total_acc.extend(np_acc * np_num_example)
                total_num_example.extend(np_num_example)
            eval_time_used = time.time() - eval_time_begin
            accum_num_example = np.sum(total_num_example)
            print(
                "[Evaluation] loss={:.5f} acc={:.5f} [step/sec: {:.2f}]".format(
                    np.sum(total_loss) / accum_num_example,
                    np.sum(total_acc) / accum_num_example,
                    eval_step / eval_time_used))

        if step % config.eval_interval == 0:
            # Final Test Block
            total_loss, total_acc, total_num_example = [], [], []
            test_data_generator = data_generator['test']
            for example in test_data_generator():
                np_loss, np_acc, np_num_example = exe.run(
                    program=test_program,
                    feed=feeder.feed([example]),
                    fetch_list=[loss, accuracy, num_example])
                total_loss.extend(np_loss * np_num_example)
                total_acc.extend(np_acc * np_num_example)
                total_num_example.extend(np_num_example)
            accum_num_example = np.sum(total_num_example)
            print("[Final Test] loss={:.5f} acc={:.5f}".format(
                np.sum(total_loss) / accum_num_example,
                np.sum(total_acc) / accum_num_example))


class Task(object):
    def __init__(self, task_type, graph_var_dict, main_program,
                 startup_program):
        self.task_type = task_type
        self.graph_var_dict = graph_var_dict
        self._main_program = main_program
        self._startup_program = startup_program

    def variable(self, var_name):
        if var_name in self.graph_var_dict:
            return self.graph_var_dict[var_name]

        raise KeyError("var_name {} not in task graph".format(var_name))

    def main_program(self):
        return self._main_program

    def startup_program(self):
        return self._startup_program