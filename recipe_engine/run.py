# Copyright (c) 2013-2015 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Entry point for fully-annotated builds.

This script is part of the effort to move all builds to annotator-based
systems. Any builder configured to use the AnnotatorFactory.BaseFactory()
found in scripts/master/factory/annotator_factory.py executes a single
AddAnnotatedScript step. That step (found in annotator_commands.py) calls
this script with the build- and factory-properties passed on the command
line.

The main mode of operation is for factory_properties to contain a single
property 'recipe' whose value is the basename (without extension) of a python
script in one of the following locations (looked up in this order):
  * build_internal/scripts/slave-internal/recipes
  * build_internal/scripts/slave/recipes
  * build/scripts/slave/recipes

For example, these factory_properties would run the 'run_presubmit' recipe
located in build/scripts/slave/recipes:
    { 'recipe': 'run_presubmit' }

TODO(vadimsh, iannucci, luqui): The following docs are very outdated.

Annotated_run.py will then import the recipe and expect to call a function whose
signature is:
  RunSteps(api, properties) -> None.

properties is a merged view of factory_properties with build_properties.

Items in iterable_of_things must be one of:
  * A step dictionary (as accepted by annotator.py)
  * A sequence of step dictionaries
  * A step generator
Iterable_of_things is also permitted to be a raw step generator.

A step generator is called with the following protocol:
  * The generator is initialized with 'step_history' and 'failed'.
  * Each iteration of the generator is passed the current value of 'failed'.

On each iteration, a step generator may yield:
  * A single step dictionary
  * A sequence of step dictionaries
    * If a sequence of dictionaries is yielded, and the first step dictionary
      does not have a 'seed_steps' key, the first step will be augmented with
      a 'seed_steps' key containing the names of all the steps in the sequence.

For steps yielded by the generator, if annotated_run enters the failed state,
it will only continue to call the generator if the generator sets the
'keep_going' key on the steps which it has produced. Otherwise annotated_run
will cease calling the generator and move on to the next item in
iterable_of_things.

'step_history' is an OrderedDict of {stepname -> StepData}, always representing
    the current history of what steps have run, what they returned, and any
    json data they emitted. Additionally, the OrderedDict has the following
    convenience functions defined:
      * last_step   - Returns the last step that ran or None
      * nth_step(n) - Returns the N'th step that ran or None

'failed' is a boolean representing if the build is in a 'failed' state.
"""

import collections
import os
import sys
import traceback

from . import loader
from . import recipe_api
from . import recipe_test_api
from . import types
from . import util


SCRIPT_PATH = os.path.dirname(os.path.abspath(__file__))

BUILDBOT_MAGIC_ENV = set([
    'BUILDBOT_BLAMELIST',
    'BUILDBOT_BRANCH',
    'BUILDBOT_BUILDBOTURL',
    'BUILDBOT_BUILDERNAME',
    'BUILDBOT_BUILDNUMBER',
    'BUILDBOT_CLOBBER',
    'BUILDBOT_GOT_REVISION',
    'BUILDBOT_MASTERNAME',
    'BUILDBOT_REVISION',
    'BUILDBOT_SCHEDULER',
    'BUILDBOT_SLAVENAME',
])

ENV_WHITELIST_WIN = BUILDBOT_MAGIC_ENV | set([
    'APPDATA',
    'AWS_CREDENTIAL_FILE',
    'BOTO_CONFIG',
    'BUILDBOT_ARCHIVE_FORCE_SSH',
    'CHROME_HEADLESS',
    'CHROMIUM_BUILD',
    'COMMONPROGRAMFILES',
    'COMMONPROGRAMFILES(X86)',
    'COMMONPROGRAMW6432',
    'COMSPEC',
    'COMPUTERNAME',
    'DBUS_SESSION_BUS_ADDRESS',
    'DEPOT_TOOLS_GIT_BLEEDING',
    # TODO(maruel): Remove once everyone is on 2.7.5.
    'DEPOT_TOOLS_PYTHON_275',
    'DXSDK_DIR',
    'GIT_USER_AGENT',
    'HOME',
    'HOMEDRIVE',
    'HOMEPATH',
    'LOCALAPPDATA',
    'NUMBER_OF_PROCESSORS',
    'OS',
    'PATH',
    'PATHEXT',
    'PROCESSOR_ARCHITECTURE',
    'PROCESSOR_ARCHITEW6432',
    'PROCESSOR_IDENTIFIER',
    'PROGRAMFILES',
    'PROGRAMW6432',
    'PWD',
    'PYTHONPATH',
    'SYSTEMDRIVE',
    'SYSTEMROOT',
    'TEMP',
    'TESTING_MASTER',
    'TESTING_MASTER_HOST',
    'TESTING_SLAVENAME',
    'TMP',
    'USERNAME',
    'USERDOMAIN',
    'USERPROFILE',
    'VS100COMNTOOLS',
    'VS110COMNTOOLS',
    'WINDIR',
])

ENV_WHITELIST_POSIX = BUILDBOT_MAGIC_ENV | set([
    'AWS_CREDENTIAL_FILE',
    'BOTO_CONFIG',
    'CCACHE_DIR',
    'CHROME_ALLOCATOR',
    'CHROME_HEADLESS',
    'CHROME_VALGRIND_NUMCPUS',
    'DISPLAY',
    'DISTCC_DIR',
    'GIT_USER_AGENT',
    'HOME',
    'HOSTNAME',
    'HTTP_PROXY',
    'http_proxy',
    'HTTPS_PROXY',
    'LANG',
    'LOGNAME',
    'PAGER',
    'PATH',
    'PWD',
    'PYTHONPATH',
    'SHELL',
    'SSH_AGENT_PID',
    'SSH_AUTH_SOCK',
    'SSH_CLIENT',
    'SSH_CONNECTION',
    'SSH_TTY',
    'TESTING_MASTER',
    'TESTING_MASTER_HOST',
    'TESTING_SLAVENAME',
    'USER',
    'USERNAME',
])


# Return value of run_steps and RecipeEngine.run.  Just a container for the
# literal return value of the recipe.
RecipeResult = collections.namedtuple('RecipeResult', 'result')


def run_steps(properties, stream_engine, step_runner, universe):
  """Runs a recipe (given by the 'recipe' property).

  Args:
    properties: a dictionary of properties to pass to the recipe.  The
      'recipe' property defines which recipe to actually run.
    stream_engine: the StreamEngine to use to create individual step streams.
    step_runner: The StepRunner to use to 'actually run' the steps.
    universe: The RecipeUniverse to use to load the recipes & modules.

  Returns: RecipeResult
  """
  # NOTE(iannucci): 'root' was a terribly bad idea and has been replaced by
  # 'patch_project'. 'root' had Rietveld knowing about the implementation of
  # the builders. 'patch_project' lets the builder (recipe) decide its own
  # destiny.
  properties.pop('root', None)

  # TODO(iannucci): A much better way to do this would be to dynamically
  #   detect if the mirrors are actually available during the execution of the
  #   recipe.
  if ('use_mirror' not in properties and (
    'TESTING_MASTERNAME' in os.environ or
    'TESTING_SLAVENAME' in os.environ)):
    properties['use_mirror'] = False

  engine = RecipeEngine(step_runner, properties, universe)

  # Create all API modules and top level RunSteps function.  It doesn't launch
  # any recipe code yet; RunSteps needs to be called.
  api = None
  with stream_engine.new_step_stream('setup_build') as s:
    assert 'recipe' in properties
    recipe = properties['recipe']

    properties_to_print = properties.copy()
    if 'use_mirror' in properties:
      del properties_to_print['use_mirror']

    run_recipe_help_lines = [
        'To repro this locally, run the following line from a build checkout:',
        '',
        './scripts/tools/run_recipe.py %s --properties-file - <<EOF' % recipe,
        repr(properties_to_print),
        'EOF',
        '',
        'To run on Windows, you can put the JSON in a file and redirect the',
        'contents of the file into run_recipe.py, with the < operator.',
    ]

    with s.new_log_stream('run_recipe') as l:
      for line in run_recipe_help_lines:
        l.write_line(line)

    _isolate_environment()

    # Find and load the recipe to run.
    try:
      recipe_script = universe.load_recipe(recipe)
      s.write_line('Running recipe with %s' % (properties,))

      api = loader.create_recipe_api(recipe_script.LOADED_DEPS,
                                     engine,
                                     recipe_test_api.DisabledTestData())

      s.add_step_text('<br/>running recipe: "%s"' % recipe)
    except loader.LoaderError as e:
      s.add_step_text('<br/>%s' % '<br/>'.join(str(e).splitlines()))
      s.set_step_status('EXCEPTION')
      return RecipeResult({
          'status_code': 2,
          'reason': str(e),
      })

  # Run the steps emitted by a recipe via the engine, emitting annotations
  # into |stream| along the way.
  return engine.run(recipe_script, api)


def _isolate_environment():
  """Isolate the environment to a known subset set."""
  if sys.platform.startswith('win'):
    whitelist = ENV_WHITELIST_WIN
  elif sys.platform in ('darwin', 'posix', 'linux2'):
    whitelist = ENV_WHITELIST_POSIX
  else:
    print ('WARNING: unknown platform %s, not isolating environment.' %
           sys.platform)
    return

  for k in os.environ.keys():
    if k not in whitelist:
      del os.environ[k]


class RecipeEngine(object):
  """
  Knows how to execute steps emitted by a recipe, holds global state such as
  step history and build properties. Each recipe module API has a reference to
  this object.

  Recipe modules that are aware of the engine:
    * properties - uses engine.properties.
    * step - uses engine.create_step(...), and previous_step_result.
  """

  ActiveStep = collections.namedtuple('ActiveStep',
                                      'step step_result open_step nest_level')

  def __init__(self, step_runner, properties, universe):
    """See run_steps() for parameter meanings."""
    self._step_runner = step_runner
    self._properties = properties
    self._universe = universe

    # A stack of ActiveStep objects, holding the most recently executed step at
    # each nest level (objects deeper in the stack have lower nest levels).
    # When we pop from this stack, we close the corresponding step stream.
    self._step_stack = []

  @property
  def properties(self):
    return self._properties

  @property
  def universe(self):
    return self._universe

  @property
  def previous_step_result(self):
    """Allows api.step to get the active result from any context.

    This always returns the innermost nested step that is still open --
    presumably the one that just failed if we are in an exception handler."""
    return self._step_stack[-1].step_result

  def _close_to_level(self, level):
    """Close all open steps that are at least as deep as level."""
    while self._step_stack and level <= self._step_stack[-1].nest_level:
      cur = self._step_stack.pop()
      if cur.step_result:
        cur.step_result.presentation.finalize(cur.open_step.stream)
      cur.open_step.finalize()

  def run_step(self, step):
    """
    Runs a step.

    Args:
      step: The step to run.

    Returns:
      A StepData object containing the result of running the step.
    """
    with util.raises((recipe_api.StepFailure, OSError),
                     self._step_runner.stream_engine):
      ok_ret = step.pop('ok_ret')
      infra_step = step.pop('infra_step')

      step_result = None

      nest_level = step.pop('step_nest_level', 0)
      self._close_to_level(nest_level)

      open_step = self._step_runner.open_step(step)
      self._step_stack.append(self.ActiveStep(
          step=step,
          step_result=None,
          open_step=open_step,
          nest_level=nest_level))
      if nest_level:
        open_step.stream.set_nest_level(nest_level)

      step_result = open_step.run()
      self._step_stack[-1] = (
          self._step_stack[-1]._replace(step_result=step_result))

      if step_result.retcode in ok_ret:
        step_result.presentation.status = 'SUCCESS'
        return step_result
      else:
        if not infra_step:
          state = 'FAILURE'
          exc = recipe_api.StepFailure
        else:
          state = 'EXCEPTION'
          exc = recipe_api.InfraFailure

        step_result.presentation.status = state

        self._step_stack[-1].open_step.stream.write_line(
            'step returned non-zero exit code: %d' % step_result.retcode)

        raise exc(step['name'], step_result)

  def run(self, recipe_script, api):
    """Run a recipe represented by a recipe_script object.

    This function blocks until recipe finishes.
    It mainly executes the recipe, and has some exception handling logic, and
    adds the step history to the result.

    Args:
      recipe_script: The recipe to run, as represented by a RecipeScript object.
      api: The api, with loaded module dependencies.
           Used by the some special modules.

    Returns:
      RecipeResult which has return value or status code and exception.
    """
    result = None

    with self._step_runner.run_context():
      try:
        try:
          recipe_result = recipe_script.run(api, api._engine.properties)
          result = {
            "recipe_result": recipe_result,
            "status_code": 0
          }
        finally:
          self._close_to_level(0)
      except recipe_api.StepFailure as f:
        result = {
          "reason": f.reason,
          "status_code": f.retcode or 1
        }
      except types.StepDataAttributeError as ex:
        result = {
          "reason": "Invalid Step Data Access: %r" % ex,
          "traceback": traceback.format_exc().splitlines(),
          "status_code": -1
        }

        raise
      except Exception as ex:
        result = {
          "reason": "Uncaught Exception: %r" % ex,
          "traceback": traceback.format_exc().splitlines(),
          "status_code": -1
        }

        raise

    result['name'] = '$result'
    return RecipeResult(result)

  def create_step(self, step):  # pylint: disable=R0201
    """Called by step module to instantiate a new step.

    Args:
      step: ConfigGroup object with information about the step, see
        recipe_modules/step/config.py.

    Returns:
      Opaque engine specific object that is understood by 'run_steps' method.
    """
    return step.as_jsonish()

  def depend_on(self, recipe, properties, distributor=None):
    return self.depend_on_multi(
        ((recipe, properties),), distributor=distributor)[0]

  def depend_on_multi(self, dependencies, distributor=None):
    results = []
    for recipe, properties in dependencies:
      recipe_script = self._universe.load_recipe(recipe)

      return_schema = getattr(recipe_script, 'RETURN_SCHEMA', None)
      if not return_schema:
        raise ValueError(
            "Invalid recipe %s. Recipe must have a return schema." % recipe)

      # run_recipe is a function which will be called once the properties have
      # been validated by the recipe engine. The arguments being passed in are
      # simply the values being passed to the recipe, which we already know, so
      # we ignore them. We're only using this for its properties validation
      # functionality.
      run_recipe = lambda *args, **kwargs: (
        self._step_runner.run_recipe(self._universe, recipe, properties))

      try:
        # This does type checking for properties
        results.append(
          loader._invoke_with_properties(
            run_recipe, properties, recipe_script.PROPERTIES,
            properties.keys()))
      except TypeError as e:
        raise TypeError(
            "Got %r while trying to call recipe %s with properties %r" % (
              e, recipe, properties))

    return results
