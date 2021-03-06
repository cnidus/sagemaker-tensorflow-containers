import argparse
import json
import subprocess
from threading import Thread
import container_support as cs
import os
import tensorflow as tf
import run
import serve
from tf_container.trainer import Trainer
import time

CHANNEL_DIR = "training"

_logger = run.get_logger()


def _wait_until_master_is_down(master):
    while True:
        try:
            # this subprocess call is python 2/3 compatible and will throw an exception when the status code is != 0
            subprocess.check_call(['curl', '{}:2222'.format(master)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            time.sleep(10)
        except subprocess.CalledProcessError:
            _logger.info("master {} is down, stopping parameter server".format(master))
            return


def save_tf_config_env_var(tf_config):
    os.environ['TF_CONFIG'] = json.dumps(tf_config)

    _logger.info('----------------------TF_CONFIG--------------------------')
    _logger.info(os.environ['TF_CONFIG'])
    _logger.info('---------------------------------------------------------')


def _run_ps_server(current_host, hosts, tf_config):
    """After the training finishes, parameter servers won't stop running because server.join() has an infinite loop.
    That is a known issue: https://github.com/tensorflow/ecosystem/issues/19
    The solution below, runs the parameter server in a secondary thread while the main thread pings the master waiting
    for it to stop responding. After that, it will exit the application gracefully given that python threads cannot be
    stopped

    Args:
        current_host: (str) name of the current host
        hosts: list (str) list of all the hostnames
        tf_config: dict (str) tensorflow config map

    Returns:
    """

    def start_ps_server(current_host, hosts, tf_config):
        cluster_spec = tf.train.ClusterSpec(tf_config['cluster'])
        task_index = hosts.index(current_host)
        server = tf.train.Server(cluster_spec, job_name='ps', task_index=task_index)
        server.join()

    t = Thread(target=start_ps_server, args=(current_host, hosts, tf_config))
    t.start()


def _get_default_training_params(env):
    my_parser = argparse.ArgumentParser()
    my_parser.add_argument('--training_steps', type=int, default=1000)
    my_parser.add_argument('--evaluation_steps', type=int, default=100)
    hp = env.argparse_hyperparameters(my_parser)

    return hp.training_steps, hp.evaluation_steps


def _get_master(tf_config):
    return tf_config['cluster']['master'][0][:-5]


def train():
    env = cs.TrainingEnvironment()

    checkpoint_dir = env.hyperparameters.get("checkpoint_path", env.model_dir)
    train_steps = env.hyperparameters.get('training_steps', 1000)
    eval_steps = env.hyperparameters.get('evaluation_steps', 100)

    # https://github.com/tensorflow/tensorflow/issues/15868
    # The default request timeout for S3, within the C++ SDK, is 3 seconds, which times out when
    # saving checkpoints of larger sizes.
    os.environ['S3_REQUEST_TIMEOUT_MSEC'] = str(env.hyperparameters.get('s3_checkpoint_save_timeout', 60000))

    env.download_user_module()
    env.pip_install_requirements()

    customer_script = env.import_user_module()

    train_wrapper = Trainer(customer_script=customer_script,
                            current_host=env.current_host,
                            hosts=env.hosts,
                            train_steps=train_steps,
                            eval_steps=eval_steps,
                            training_path=env.channel_dirs[CHANNEL_DIR],
                            model_path=checkpoint_dir,
                            output_path=env.output_dir,
                            customer_params=env.hyperparameters)

    tf_config = train_wrapper.build_tf_config()

    # only creating a parameter servers for distributed runs
    if len(env.hosts) > 1:
        _run_ps_server(env.current_host, env.hosts, tf_config)

    save_tf_config_env_var(tf_config)

    try:
        run.train_and_log_exceptions(train_wrapper, env.output_dir)

        # only the master should export the model at the end of the execution
        if checkpoint_dir != env.model_dir and train_wrapper.task_type == 'master':
            serve.export_saved_model(checkpoint_dir, env.model_dir)

        if train_wrapper.task_type != 'master':
            _wait_until_master_is_down(_get_master(tf_config))
    finally:
        # Since threads in Python cannot be stopped, this is the only way to stop the application
        # https://stackoverflow.com/questions/9591350/what-is-difference-between-sys-exit0-and-os-exit0
        os._exit(0)
