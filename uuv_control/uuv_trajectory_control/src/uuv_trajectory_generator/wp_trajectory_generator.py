# Copyright (c) 2016 The UUV Simulator Authors.
# All rights reserved.
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

import rospy
import numpy as np
from copy import deepcopy
from .trajectory_point import TrajectoryPoint
from .waypoint import Waypoint
from .waypoint_set import WaypointSet
from tf.transformations import quaternion_multiply, quaternion_inverse, \
    quaternion_conjugate
from path_generator import PathGenerator
import logging
import sys


class WPTrajectoryGenerator(object):
    """
    Class that generates a trajectory from the interpolated path generated
    from a set of waypoints. It uses the information given for the waypoint's
    maximum forward speed to estimate the velocity between waypoint and
    parametrize the interpolated curve.
    The velocity and acceleration profiles are the generated through finite
    discretization. These profiles are not optimized, this class is a
    simple solution for quick trajectory generation for waypoint navigation.
    """

    def __init__(self, full_dof=False, use_finite_diff=True,
                 interpolation_method='cubic_interpolator'):
        """Class constructor."""
        self._logger = logging.getLogger('wp_trajectory_generator')
        out_hdlr = logging.StreamHandler(sys.stdout)
        out_hdlr.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(module)s | %(message)s'))
        out_hdlr.setLevel(logging.INFO)
        self._logger.addHandler(out_hdlr)
        self._logger.setLevel(logging.INFO)

        self._path_generators = dict()
        for gen in PathGenerator.get_all_generators():
            self._path_generators[gen.get_label()] = gen
            self._path_generators[gen.get_label()].set_full_dof(full_dof)
        # Time step between interpolated samples
        self._dt = None
        # Last time stamp
        self._last_t = None
        # Last interpolated point
        self._last_pnt = None
        self._this_pnt = None

        self._t_step = 0.001

        # Interpolation method
        self._interp_method = interpolation_method

        # True if the path is generated for all degrees of freedom, otherwise
        # the path will be generated for (x, y, z, yaw) only
        self._is_full_dof = full_dof

        # Use finite differentiation if true, otherwise use motion regression
        # algorithm
        self._use_finite_diff = use_finite_diff
        # Time window used for the regression method
        self._regression_window = 0.5
        # If the regression method is used, adjust the time step
        if not self._use_finite_diff:
            self._t_step = self._regression_window / 30

        # Flags to indicate that the interpolation process has started and
        # ended
        self._has_started = False
        self._has_ended = False

        # The parametric variable to use as input for the interpolator
        self._cur_s = 0

    def __del__(self):
        # Removing logging message handlers
        while self._logger.handlers:
            self._logger.handlers.pop()

    @property
    def started(self):
        """Return true if the interpolation has started."""
        return self._has_started

    @property
    def closest_waypoint(self):
        """Return the closest waypoint to the current position on the path."""
        return self._path_generators[self._interp_method].closest_waypoint

    @property
    def closest_waypoint_idx(self):
        """
        Return the index of the closest waypoint to the current position on the
        path.
        """
        return self._path_generators[self._interp_method].closest_waypoint_idx

    @property
    def interpolator(self):
        return self._path_generators[self._interp_method]

    @property
    def use_finite_diff(self):
        return self._use_finite_diff

    @use_finite_diff.setter
    def use_finite_diff(self, flag):
        assert type(flag) == bool
        self._use_finite_diff = flag

    def get_interpolation_method(self):
        return self._interp_method

    def set_interpolation_method(self, method):
        if method in self._path_generators:
            self._interp_method = method
            self._logger.info('Interpolation method set: ' + method)
            return True
        else:
            self._logger.info('Invalid interpolation method, keeping the current method <%s>' % self._interp_method)
            return False

    def is_full_dof(self):
        """Return true if the trajectory is generated for all 6 degrees of
        freedom.
        """
        return self._is_full_dof

    def get_max_time(self):
        """Return maximum trajectory time."""
        return self.interpolator.max_time

    def set_max_time(self, max_time):
        """Set a new maximum trajectory time."""
        if max_time > 0:
            self.interpolator.max_time = max_time
            self.interpolator.s_step = self._t_step / self.interpolator.max_time
            self._logger.info('New duration, max. relative time=%.2f s' % self.interpolator.max_time)
            return True
        else:
            self._logger.info('Invalid max. time, time=%.2f s' % max_time)
            return False

    def is_finished(self):
        """Return true if the trajectory has finished."""
        return self._has_ended

    def reset(self):
        """Reset all class attributes to allow a new trajectory to be
        computed.
        """
        self._dt = None
        self._last_t = None
        self._last_pnt = None
        self._this_pnt = None
        self._has_started = False
        self._has_ended = False
        self._cur_s = 0

    def init_waypoints(self, waypoint_set):
        """Initialize the waypoint set."""
        return self.interpolator.init_waypoints(waypoint_set)

    def add_waypoint(self, waypoint, add_to_beginning=False):
        """Add waypoint to the existing waypoint set. If no waypoint set has
        been initialized, create new waypoint set structure and add the given
        waypoint."""
        return self.interpolator.add_waypoint(waypoint, add_to_beginning)

    def get_waypoints(self):
        """Return waypoint set."""
        return self.interpolator.waypoints

    def update_dt(self, t):
        """Update the time stamp."""
        if self._last_t is None:
            self._last_t = t
            self._dt = 0.0
            if self.interpolator.start_time is None:
                self.interpolator.start_time = t
            return False
        self._dt = t - self._last_t
        self._last_t = t
        return (True if self._dt > 0 else False)

    def get_samples(self, step=0.005):
        """Return pose samples from the interpolated path."""
        assert step > 0, 'Step size must be positive'
        return self.interpolator.get_samples(
            self.interpolator.max_time, step)

    def set_start_time(self, t):
        """Set a custom starting time to the interpolated trajectory."""
        assert t >= 0, 'Starting time must be positive'
        self.interpolator.start_time = t
        self._logger.info('Setting new starting time, t=%.2f s' % t)

    def _motion_regression_1d(self, pnts, t):
        """
        Computation of the velocity and acceleration for the target time t
        using a sequence of points with time stamps for one dimension. This
        is an implementation of the algorithm presented by [1].

        [1] Sittel, Florian, Joerg Mueller, and Wolfram Burgard. Computing
            velocities and accelerations from a pose time sequence in
            three-dimensional space. Technical Report 272, University of
            Freiburg, Department of Computer Science, 2013.
        """

        sx = 0.0
        stx = 0.0
        st2x = 0.0
        st = 0.0
        st2 = 0.0
        st3 = 0.0
        st4 = 0.0
        for pnt in pnts:
            ti = pnt[1] - t
            sx += pnt[0]
            stx += pnt[0] * ti
            st2x += pnt[0] * ti**2
            st += ti
            st2 += ti**2
            st3 += ti**3
            st4 += ti**4

        n = len(pnts)
        A = n * (st3 * st3 - st2 * st4) + \
            st * (st * st4 - st2 * st3) + \
            st2 * (st2 * st2 - st * st3)

        if A == 0.0:
            return 0.0, 0.0

        v = (1.0 / A) * (sx * (st * st4 - st2 * st3) +
                         stx * (st2 * st2 - n * st4) +
                         st2x * (n * st3 - st * st2))

        a = (2.0 / A) * (sx * (st2 * st2 - st * st3) +
                         stx * (n * st3 - st * st2) +
                         st2x * (st * st - n * st2))
        return v, a

    def _motion_regression_6d(self, pnts, qt, t):
        """
        Compute translational and rotational velocities and accelerations in
        the inertial frame at the target time t.

        [1] Sittel, Florian, Joerg Mueller, and Wolfram Burgard. Computing
            velocities and accelerations from a pose time sequence in
            three-dimensional space. Technical Report 272, University of
            Freiburg, Department of Computer Science, 2013.
        """

        lin_vel = np.zeros(3)
        lin_acc = np.zeros(3)

        q_d = np.zeros(4)
        q_dd = np.zeros(4)

        for i in range(3):
            v, a = self._motion_regression_1d(
                [(pnt['pos'][i], pnt['t']) for pnt in pnts], t)
            lin_vel[i] = v
            lin_acc[i] = a

        for i in range(4):
            v, a = self._motion_regression_1d(
                [(pnt['rot'][i], pnt['t']) for pnt in pnts], t)
            q_d[i] = v
            q_dd[i] = a

        # Keeping all velocities and accelerations in the inertial frame
        ang_vel = 2 * quaternion_multiply(q_d, quaternion_conjugate(qt))
        ang_acc = 2 * quaternion_multiply(q_dd, quaternion_conjugate(qt))

        return np.hstack((lin_vel, ang_vel[0:3])), np.hstack((lin_acc, ang_acc[0:3]))

    def generate_pnt(self, s=None):
        """Return trajectory sample for the current parameter s."""
        cur_s = (self._cur_s if s is None else s)
        last_s = cur_s - self.interpolator.s_step
        # Generate position and rotation quaternion for the current path
        # generator method
        pnt = self.interpolator.generate_pnt(
            cur_s, cur_s * (self.interpolator.max_time - self.interpolator.start_time) + self.interpolator.start_time)
        if self._use_finite_diff:
            # Set linear velocity
            pnt.vel = self._generate_vel(cur_s)
            # Compute linear and angular accelerations
            last_vel = self._generate_vel(last_s)
            pnt.acc = (pnt.vel - last_vel) / self._t_step
        else:
            pnts = list()
            for ti in np.arange(pnt.t - self._regression_window / 2, pnt.t + self._regression_window, self._t_step):
                if ti < 0:
                    si = 0
                elif ti > self.interpolator.max_time - self.interpolator.start_time:
                    si = 1
                else:
                    si = (ti - self.interpolator.start_time) / self.interpolator.max_time
                pnts.append(dict(pos=self.interpolator.generate_pos(si),
                                 rot=self.interpolator.generate_quat(si),
                                 t=ti))
            vel, acc = self._motion_regression_6d(pnts, pnt.rotq, pnt.t)
            pnt.vel = vel
            pnt.acc = acc
        return pnt

    def _generate_vel(self, s=None):
        cur_s = (self._cur_s if s is None else s)
        last_s = cur_s - self.interpolator.s_step

        if last_s < 0 or cur_s > 1:
            return np.zeros(6)

        q_cur = self.interpolator.generate_quat(cur_s)
        q_last = self.interpolator.generate_quat(last_s)

        cur_pos = self.interpolator.generate_pos(cur_s)
        last_pos = self.interpolator.generate_pos(last_s)

        ########################################################
        # Computing angular velocities
        ########################################################
        # Quaternion difference to the last step in the inertial frame
        q_diff = quaternion_multiply(q_cur, quaternion_inverse(q_last))
        # Angular velocity
        ang_vel = 2 * q_diff[0:3] / self._t_step

        vel = [(cur_pos[0] - last_pos[0]) / self._t_step,
               (cur_pos[1] - last_pos[1]) / self._t_step,
               (cur_pos[2] - last_pos[2]) / self._t_step,
               ang_vel[0],
               ang_vel[1],
               ang_vel[2]]
        return np.array(vel)

    def interpolate(self, t):
        if not self._has_started:
            if not self.interpolator.init_interpolator():
                return None
            self.interpolator.s_step = self._t_step / (self.interpolator.max_time - self.interpolator.start_time)
            self.update_dt(t)
            # Generate first point
            self._cur_s = 0
            self._has_started = True
            self._has_ended = False

        if t > self.interpolator.max_time or t - self.interpolator.start_time < 0:
            self._has_started = False
            if t > self.interpolator.max_time:
                self._has_ended = True
        else:
            self._has_started = True
            self._has_ended = False

        # Retrieving current position and heading
        self._cur_s = (t - self.interpolator.start_time) / (self.interpolator.max_time - self.interpolator.start_time)
        self._this_pnt = self.generate_pnt()
        self._this_pnt.t = t

        self._last_pnt = deepcopy(self._this_pnt)
        return self._this_pnt