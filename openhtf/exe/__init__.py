# Copyright 2014 Google Inc. All Rights Reserved.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""TestExecutor executes tests."""

import logging
import sys
import threading
import time
from enum import Enum

import contextlib2 as contextlib

import openhtf
from openhtf import conf
from openhtf import plugs
from openhtf import util
from openhtf.exe import phase_executor
from openhtf.exe import test_state
from openhtf.io import test_record
from openhtf.io import user_input
from openhtf.util import threads

_LOG = logging.getLogger(__name__)

conf.Declare('teardown_timeout_s', default_value=3, description=
    'Timeout (in seconds) for test teardown functions.')


class TestStopError(Exception):
  """Test is being stopped."""


class UnfinalizedTestError(Exception):
  """A Test wasn't finalized, likely we missed an abort condition."""


# pylint: disable=too-many-instance-attributes
class TestExecutor(threads.KillableThread):
  """Encompasses the execution of a single test."""

  def __init__(self, test_descriptor, test_start, teardown_function=None):
    super(TestExecutor, self).__init__(name='TestExecutorThread')

    self._teardown_function = None
    if teardown_function:
      self._teardown_function = openhtf.PhaseDescriptor.WrapOrCopy(
          teardown_function)
      # Force teardown function timeout.
      self._teardown_function.options.timeout_s = conf.teardown_timeout_s

    self._test_descriptor = test_descriptor
    self._test_start = test_start
    self._lock = threading.Lock()

  def Start(self):
    """Style-compliant start method."""
    self.start()

  def Stop(self):
    """Stop this test."""
    _LOG.info('Stopping test executor.')
    with self._lock:
      if self._exit_stack:
        self._exit_stack.close()
    self.Kill()

  def Finalize(self):
    """Finalize test execution and output resulting record to callbacks.

    Should only be called once at the conclusion of a test run, and will raise
    an exception if end_time_millis is already set.

    Returns: Finalized TestState.  It should not be modified after this call.

    Raises: TestAlreadyFinalized if end_time_millis already set.
    """
    if not self._test_state:
      raise TestStopError('Test Stopped.')

    if not self._test_state.is_finalized:
      self._test_state.logger.info('Finishing test with outcome ABORTED.')
      self._test_state.Finalize(test_record.Outcome.ABORTED)

    return self._test_state
    
  def Wait(self):
    """Waits until death."""
    try:
      self.join(sys.float_info.max)  # Timeout needed for SIGINT handling.
    except KeyboardInterrupt:
      self._test_state.logger.info(
          'KeyboardInterrupt caught, finishing test with outcome ABORTED.')
      self._test_state.Finalize(test_record.Outcome.ABORTED)
      raise

  def GetState(self):
    """Return the current TestState object."""
    return self._test_state

  def _ThreadProc(self):
    """Handles one whole test from start to finish."""
    with self._lock:
      self._exit_stack = None
    self._test_state = None
    self._output_thread = None

    with contextlib.ExitStack() as exit_stack:
      # Top level steps required to run a single iteration of the Test.
      _LOG.info('Starting test %s', self._test_descriptor.code_info.name)

      # Any access to self._exit_stack must be done while holding this lock.
      with self._lock:
        # Initial setup of exit stack and final cleanup of attributes.
        self._exit_stack = exit_stack

      self._test_state = test_state.TestState(self._test_descriptor)
      # Wait here until the test start trigger returns a DUT ID.  Don't hold
      # self._lock while we do this, or else calls to Stop() will deadlock.
      # Create plugs while we're here because that may also take a while and
      # we don't want to hold self._lock while we wait.
      self._test_state.TestStarted(self._WaitForTestStart())
      self._test_state.plug_manager.InitializePlugs()

      with self._lock:
        if not self._exit_stack:
          # We shouldn't get here, but just in case something went weird with a
          # call to Stop() and we ended up resuming execution here but the
          # exit stack was already cleared, bail.  Try to tear down plugs on a
          # best-effort basis.
          self._test_state.plug_manager.TearDownPlugs()
          raise TestStopError('Test Stopped.')

        # Tear down plugs first, then output test record.
        exit_stack.callback(self._test_state.plug_manager.TearDownPlugs)

        # Perform initialization of some top-level stuff we need.
        executor = self._MakePhaseExecutor(exit_stack)

      # Everything is set, set status and begin test execution.  Note we don't
      # protect this with a try: block because the PhaseExecutor handles any
      # exceptions from test code.  Any exceptions here are caused by the
      # framework, and we probably want them to interrupt framework state
      # changes (like the transition to FINISHING).
      self._ExecuteTestPhases(executor)

  def _WaitForTestStart(self):
    """Wait for the test start trigger to return a DUT ID."""
    if self._test_start is None:
      return
    return self._test_start()

  def _MakePhaseExecutor(self, exit_stack):
    """Create a phase_executor.PhaseExecutor and set it up."""
    executor = phase_executor.PhaseExecutor(self._test_state)
    exit_stack.callback(executor.Stop)
    return executor

  def _ExecuteTestPhases(self, executor):
    """Executes one test's phases from start to finish."""
    self._test_state.SetStatusRunning()

    try:
      for phase_outcome in executor.ExecutePhases(
          self._test_descriptor.phases):
        if self._test_state.SetStatusFromPhaseOutcome(phase_outcome):
          break
      else:
        self._test_state.Finalize()
    except KeyboardInterrupt:
      self._test_state.logger.info(
          'KeyboardInterrupt caught, finishing test with outcome ABORTED.')
      self._test_state.Finalize(test_record.Outcome.ABORTED)
      raise

    # Run teardown function. TODO(madsci): Rethink this, it has to happen
    # before the TestState is Finalize()'d.
    if self._teardown_function:
      executor._ExecuteOnePhase(self._teardown_function, skip_record=True)
