import logging

import boto3
import os
import tensorflow as tf
from container_support import parse_s3_url
from tensorflow.contrib.learn import RunConfig, Experiment
from tensorflow.contrib.learn.python.learn import learn_runner
from tensorflow.contrib.learn.python.learn.utils import saved_model_export_utils

logger = logging.getLogger(__name__)


class Trainer(object):
    def __init__(self,
                 customer_script,
                 current_host,
                 hosts,
                 train_steps=1000,
                 eval_steps=100,
                 training_path=None,
                 model_path=None,
                 output_path=None,
                 min_eval_frequency=1000,
                 customer_params={},
                 save_checkpoints_secs=300):
        """

        Args:
            customer_script: (module) Customer loaded module
            current_host: (str) Current hostname
            hosts: list (str) List with all containers list names
            train_steps: (int) Perform this many steps of training. 'None', the default,
                means train forever.
            eval_steps: (int) 'evaluate' runs until input is exhausted (or another exception
                is raised), or for 'eval_steps' steps, if specified.
            training_path: (str) Base output directory
            model_path: (str) Directory where checkpoints will be saved. Can be a S3 bucket
            output_path: (str) Local directory where the model will be saved
            min_eval_frequency: (int) Applies only to master container. the minimum
                number of steps between evaluations. Of course, evaluation does not
                occur if no new snapshot is available, hence, this is the minimum.
                If 0, the evaluation will only happen after training.
                Defaults to 1000.
        """
        self.customer_script = customer_script
        self.current_host = current_host
        self.hosts = hosts
        self.train_steps = train_steps
        self.eval_steps = eval_steps
        self.training_path = training_path
        self.model_path = model_path
        self.ouput_path = output_path
        self.task_type = None

        customer_params['min_eval_frequency'] = customer_params.get('min_eval_frequency', min_eval_frequency)
        customer_params['save_checkpoints_secs'] = customer_params.get('save_checkpoints_secs', save_checkpoints_secs)

        self.customer_params = customer_params

        if model_path.startswith('s3://'):
            self._configure_s3_file_system()

    def _get_task_type(self, masters):
        if self.current_host in masters:
            return 'master'
        return 'worker'

    def build_tf_config(self):
        """Builds a dictionary containing cluster information based on number of hosts and number of parameter servers.
        More information about TF_Config: https://github.com/tensorflow/tensorflow/blob/master/tensorflow/contrib/learn
        /python/learn/estimators/run_config.py#L77
        :return: task_type and tf_config dictionary
        """

        masters = self.hosts[:1]
        workers = self.hosts[1:]
        ps = self.hosts[:] if len(self.hosts) > 1 else None

        self.task_type = self._get_task_type(masters)

        task_map = {'master': masters, 'worker': workers}

        if ps:
            task_map['ps'] = ps

        task_id = task_map[self.task_type].index(self.current_host)

        def build_host_addresses(my_hosts, port='2222'):
            return ['{}:{}'.format(host, port) for host in my_hosts]

        tf_config = {
            "cluster": {
                "master": build_host_addresses(masters),
            },
            "task": {
                "index": task_id,
                "type": self.task_type
            },
            "environment": 'cloud'
        }

        if ps:
            tf_config['cluster']['ps'] = build_host_addresses(ps, port='2223')

        if len(workers) > 0:
            tf_config['cluster']['worker'] = build_host_addresses(workers)

        return tf_config

    def train(self):
        experiment_fn = self._generate_experiment_fn()
        learn_runner.run(experiment_fn, self.training_path)

    def _generate_experiment_fn(self):
        estimator = self._build_estimator()

        def _experiment_fn(output_dir):
            valid_experiment_keys = ['eval_metrics', 'train_monitors', 'eval_hooks', 'local_eval_frequency',
                                     'eval_delay_secs', 'continuous_eval_throttle_secs', 'min_eval_frequency',
                                     'delay_workers_by_global_step', 'train_steps_per_iteration']

            experiment_params = {k: v for k, v in self.customer_params.items() if k in valid_experiment_keys}

            logging.info("creating Experiment:")
            logging.info(experiment_params)

            '''
            TensorFlow input functions (train_input_fn, and eval_input_fn) can return features and
            labels, or a function that returns features and labels
            Examples of valid input functions:

                def train_input_fn(training_dir, hyperparameters):
                    ...
                    return tf.estimator.inputs.numpy_input_fn(x={"x": train_data}, y=train_labels)

                def train_input_fn(training_dir, hyperparameters):
                    ...
                    return features, labels
            '''
            def _train_input_fn():
                return _function(self.customer_script.train_input_fn(self.training_path, self.customer_params))()

            def _eval_input_fn():
                return _function(self.customer_script.eval_input_fn(self.training_path, self.customer_params))()

            '''
            TensorFlow serving input functions (serving_input_fn) can return a ServingInputReceiver object or a
            function that a ServingInputReceiver
            Examples of valid serving input functions:

                def serving_input_fn(params):
                    feature_spec = {INPUT_TENSOR_NAME: tf.FixedLenFeature(dtype=tf.float32, shape=[4])}
                    return tf.estimator.export.build_parsing_serving_input_receiver_fn(feature_spec)

                def serving_input_fn(hyperpameters):
                    inputs = {INPUT_TENSOR_NAME: tf.placeholder(tf.float32, [None, 32, 32, 3])}
                    return tf.estimator.export.ServingInputReceiver(inputs, inputs)
            '''
            def _serving_input_fn():
                return _function(self.customer_script.serving_input_fn(self.customer_params))()

            return Experiment(
                estimator=estimator,
                train_input_fn=_train_input_fn,
                eval_input_fn=_eval_input_fn,
                export_strategies=[saved_model_export_utils.make_export_strategy(
                    serving_input_fn=_serving_input_fn,
                    default_output_alternative_key=None,
                    exports_to_keep=1)],
                train_steps=self.train_steps,
                eval_steps=self.eval_steps,
                **experiment_params
            )

        return _experiment_fn

    def _build_estimator(self):
        valid_runconfig_keys = ['save_summary_steps', 'save_checkpoints_secs', 'save_checkpoints_steps',
                                'keep_checkpoint_max', 'keep_checkpoint_every_n_hours', 'log_step_count_steps']

        runconfig_params = {k: v for k, v in self.customer_params.items() if k in valid_runconfig_keys}

        logging.info("creating RunConfig:")
        logging.info(runconfig_params)

        run_config = RunConfig(
            model_dir=self.model_path,
            **runconfig_params
        )

        if hasattr(self.customer_script, 'estimator_fn'):
            logging.info("invoking estimator_fn")
            return self.customer_script.estimator_fn(run_config, self.customer_params)
        elif hasattr(self.customer_script, 'keras_model_fn'):
            logging.info("involing keras_model_fn")
            model = self.customer_script.keras_model_fn(self.customer_params)
            return tf.keras.estimator.model_to_estimator(keras_model=model, config=run_config)
        else:
            logging.info("creating the estimator")

            # transforming hyperparameters arg to params, which is required by tensorflow
            def _model_fn(features, labels, mode, params):
                return self.customer_script.model_fn(features, labels, mode, params)

            return tf.estimator.Estimator(
                model_fn=_model_fn,
                params=self.customer_params,
                config=run_config)

    def _configure_s3_file_system(self):
        # loads S3 filesystem plugin
        s3 = boto3.client('s3')

        bucket_name, key = parse_s3_url(self.model_path)

        bucket_location = s3.get_bucket_location(Bucket=bucket_name)['LocationConstraint']

        if bucket_location:
            os.environ['S3_REGION'] = bucket_location
        os.environ['S3_USE_HTTPS'] = "1"


def _function(object):
    """Ensures that the object is a function. Wraps the object in a function otherwise.
    Args:
        object: object to be wrapped as function

    Returns: function with the wrapped object.
    """
    if hasattr(object, '__call__'):
        return object

    return lambda: object
