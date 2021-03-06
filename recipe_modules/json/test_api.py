# Copyright 2015 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import json

from recipe_engine import recipe_test_api

class JsonTestApi(recipe_test_api.RecipeTestApi):
  @recipe_test_api.placeholder_step_data
  @staticmethod
  def output(data, retcode=None):
    return json.dumps(data), retcode

  def output_stream(self, data, stream='stdout', retcode=None):
    assert stream in ('stdout', 'stderr')
    ret = recipe_test_api.StepTestData()
    setattr(ret, stream, self.output(data, retcode).unwrap_placeholder())
    return ret
