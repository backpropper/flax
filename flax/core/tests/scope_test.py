# Copyright 2020 The Flax Authors.
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

from flax.core import Scope, init, apply

from jax import random

import numpy as np


from absl.testing import absltest

class ScopeTest(absltest.TestCase):

  def test_rng(self):
    def f(scope):
      self.assertTrue(scope.has_rng('param'))
      self.assertFalse(scope.has_rng('dropout'))
      rng = scope.make_rng('param')
      self.assertTrue(np.all(rng == random.fold_in(random.PRNGKey(0), 1)))

    init(f)(random.PRNGKey(0))

  


if __name__ == '__main__':
  absltest.main()
