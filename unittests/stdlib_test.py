#!/usr/bin/env python
# Copyright 2015 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Runs simulation tests and lint on the standard recipe modules."""

import os
import subprocess

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def recipes_py(*args):
  subprocess.check_call([
      os.path.join(ROOT_DIR, 'recipes.py'),
      '--package', os.path.join(ROOT_DIR, 'infra', 'config', 'recipes.cfg')] +
      list(args))

recipes_py('simulation_test')
recipes_py('lint')
