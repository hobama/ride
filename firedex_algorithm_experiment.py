#! /usr/bin/env python

# @author: Kyle Benson
# (c) Kyle Benson 2018

import json
import os
import subprocess
# For working with the temp configuration file
import tempfile

from firedex_experiment import FiredexExperiment
from scifire.algorithms.firedex_algorithm import FiredexAlgorithm  # just for type hinting
from scifire.firedex_configuration import QueueStabilityError
from scifire.algorithms import build_algorithm
from scifire.defaults import *
import logging
log = logging.getLogger(__name__)


class FiredexAlgorithmExperiment(FiredexExperiment):
    """
    Simulation-based experiments that run in our Java-based queuing network simulation.
    """

    def __init__(self, regen_bad_ros=False, testing=False, **kwargs):
        """
        :param regen_bad_ros: regenerate the configuration if the ro condition is not met (default=False)
        :param testing: when explicitly set to True, don't run external simulator; used for just viewing random configs
        :param kwargs: passed to super constructor
        """
        super(FiredexAlgorithmExperiment, self).__init__(**kwargs)

        # 0 priority levels means we aren't assigning ANY priorities!
        if self.num_priority_levels > 0:
            alg_cfg = self.algorithm  # type: dict
            self.algorithm = build_algorithm(**alg_cfg)  # type: FiredexAlgorithm
        else:
            self.algorithm = build_algorithm(algorithm='null')  # type: FiredexAlgorithm

        self.regen_bad_ros = regen_bad_ros
        self.testing = testing

    def run_experiment(self):
        """Run the algorithm on our current scenario and feed the configuration to a queuing network simulator
        to determine its performance under these 'ideal' network settings."""

        # Setup a compact data model of our formulation that's used to configure external queue simulator experiment.
        # XXX: need to check ro to ensure queue stability as otherwise simulator can't run!
        ros_okay = False
        retries_left = 1000
        while not ros_okay and retries_left > 0 and self.regen_bad_ros:
            # ro_tolerance may be used to violate this check slightly, but the queue simulator will not run if it's violated!
            ros_okay = self.algorithm.ros_okay(self, tolerance=0.0)
            if not ros_okay:
                self.generate_configuration()
                retries_left -= 1
                if retries_left % 100 == 99:
                    log.info("RO condition not met: regenerating configuration...")

        if retries_left == 0 and self.regen_bad_ros:
            log.error("failed to generate configuration that satisfies RO condtition after 1000 retries... check params!")
            return dict(error="bad ros from configuration", ros=self.algorithm.get_ros(self))

        # Now that we have a good configuration, we can proceed with experiments:
        # Since we're running the queuing simulator for a static configuration, we just assign the topic priorities statically
        # NOTE: since the exp class IS a config class, just pass self and the alg can ignore exp-specific parts
        try:
            self.algorithm.force_update()  # for multiple runs
            prios = self.algorithm.get_subscription_priorities(self)
            cfg = self.get_simulator_input_dict(prios)

        except QueueStabilityError as e:
            ret = dict(error="bad ros from algorithm")
            log.error("Algorithm failed ro condition check: %s" % e)
            try:
                ret['ros'] = self.algorithm.get_ros(self)
                # try to save the assigned drop rates for inspection
                ret['drop_rates'] = self.algorithm.get_drop_rates(self)
            except QueueStabilityError:
                pass
            return ret

        # Since we're running an external queuing simulator, make a temporary file for passing experiment configuration
        cfg_file, cfg_filename = tempfile.mkstemp('firedex_sim_cfg', text=True)
        with os.fdopen(cfg_file, 'w') as f:
            f.write(json.dumps(cfg))
        log.info("temp input config filename for external simulator: %s" % cfg_filename)

        # Generate an output filename for the simulator based on the output filename we're using
        sim_out_fname = os.path.join(self.outputs_dir, "sim_output.csv")

        sim_jar_file = os.path.join('scifire', 'queue_simulator', 'pubsub-prio.jar')
        if not os.path.exists(sim_jar_file):
            log.error("cannot find the simulation JAR file! Make sure you download/compile it and put it at %s" % sim_jar_file)
        cmd = "java -cp %s pubsubpriorities.PubsubV6Sim %s %s" % (sim_jar_file, cfg_filename, sim_out_fname)

        # redirect to log files so if we run multiple sims in parallel via run.py they don't overlap; also can view it later now
        cmd = self.redirect_output_to_log(cmd, "sim_stdout.log")

        log.debug("Sim config: %s" % str(cfg))
        if self.testing:
            ret_code = 2
        else:
            log.info("starting external queuing simulator...")
            ret_code = subprocess.call(cmd, shell=True)

        # Delete the temp file since the configuration is saved in the results anyway
        os.remove(cfg_filename)

        # Calculate the expected performance using the analytical model in order to determine its accuracy
        expected_service_delays = self.algorithm.service_delays(self)
        expected_total_delays = self.algorithm.total_delays(self)
        expected_delivery_rates = self.algorithm.delivery_rates(self)
        expected_utilities = self.algorithm.estimate_utilities(self)

        result = dict(return_code=ret_code, sim_config=cfg, output_file=sim_out_fname,
                      exp_srv_delay=expected_service_delays, exp_total_delay= expected_total_delays,
                      exp_delivery=expected_delivery_rates, utility_weights=self.subscription_utility_weights,
                      exp_utilities=expected_utilities)

        # save some extra parameters if an error occurs
        if ret_code:
            result['ro_sums'] = [sum(ros) for ros in self.algorithm.get_ros(self)]

        return result

    def get_simulator_input_dict(self, priorities=None):
        """
        Generates a dict that represents the system configuration parameters used for running a queuing network-based
        simulation experiment.

        NOTE: lambdas are the total (over all publishers) arrival rates at the broker of each topic

        :param priorities: priority mapping taken from algorithm.get_topic_priorities() (default=None)
        :return: a dict of configuration parameters e.g.:
          {
            "lambdas": [topic1_pub_rate, topic2_pub_rate, ...],
            "mus": [topic1_service_rate, topic2_service_rate, ...],
            "error_rate": 0.1,
            "subscriptions": [0, 2, 3, 5, 8],    # for all subscribers!
            "priorities": [0, 0, 1, 2, ...],     # one for EACH topic even if not subscribed
            "prio_probs": [1.0, 0.9, 0.8, 0.7],  # rate of events let through for each priority class
          }
        """

        # Since the simulator just takes the arrival rates and doesn't actually model publishers, we need to scale the
        # arrival rates based on the number of publishers on each of those topics i.e. lambda[i] = pub_rate[i]*npubs_on_i
        # NOTE: these must all be floats as the Java queuing simulator's JSON parser has issues casting properly
        lambdas = self.algorithm.broker_arrival_rates(self)

        # priorities expected just as a list enumerating the priority class of each topic in order
        if priorities is not None:
            # since we have per-subscription priorities, we need to fill in dummy values for the non-subscribed topics
            # convert to topic dict first
            priorities = {sub.topic: priorities[sub] for sub in priorities}
            for topic in self.topics:
                # TODO: set NO PRIO instead of lowest one
                priorities.setdefault(topic, self.prio_classes[-1])

            priorities = list(sorted(priorities.items()))
            priorities = [p for t,p in priorities]

            # XXX: since the simulator determines the # prios by just getting the max of those specified, we should do:
            nprios = max(priorities) + 1

            assert self.num_topics == len(priorities), \
                "expected %d priorities (one for each topic) but got %d" % (self.num_topics, len(priorities))
        else:
            nprios = self.num_priority_levels

        # need to reverse lookup the network flow from the priority classes to tell the simulator drop rates for each
        # priority, but this could be an issue:
        if self.num_priority_levels < self.num_net_flows:
            log.warning("The queuing simulator only takes priorities, but drop rates are defined per network flow and"
                        "we have more flows than priority classes!  This might cause problems...")

        # XXX: instead, let's just assume we can just do the following:
        prio_probs = dict()
        nf_prios = self.algorithm.get_net_flow_priorities(self)
        for flow in self.net_flows:
            drop_rate = self.algorithm.get_drop_rates(self)[flow]
            v = 1.0 - drop_rate
            p = nf_prios[flow]

            if p in prio_probs and prio_probs[p] != v:
                log.warning("prio_prob[%d]=%f but now we're changing it to %f: something may be wrong!" % (p, prio_probs[p], v))
            prio_probs[p] = v

        # turn it into a list ordered by priority class
        prio_probs = [prob for prio, prob in sorted(prio_probs.items())]

        # XXX: to resolve a potential issue for if we don't generate all prio_probs:
        while len(prio_probs) < nprios:
            prio_probs.append(0.0)  # drop all traffic of this prio, which shouldn't actually be ANY!

        return dict(mus=self.service_rates, lambdas=lambdas, subscriptions=self.subscription_topics,
                    priorities=priorities, error_rate=float(self.error_rate), prio_probs=prio_probs)

    @classmethod
    def build_from_args(cls, args):
        """Constructs from command line arguments."""

        args = cls.get_arg_parser().parse_args(args)

        # convert to plain dict
        args = vars(args)

        return cls(**args)


if __name__ == "__main__":
    import sys
    exp = FiredexAlgorithmExperiment.build_from_args(sys.argv[1:])
    exp.run_all_experiments()
