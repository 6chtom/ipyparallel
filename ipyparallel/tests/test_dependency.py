"""Tests for dependency.py"""

# Copyright (c) IPython Development Team.
# Distributed under the terms of the Modified BSD License.
import ipyparallel as ipp
from ipyparallel.serialize import can, uncan
from ipyparallel.util import interactive

from .clienttest import ClusterTestCase, raises_remote


@ipp.require('time')
def wait(n):
    time.sleep(n)  # noqa: F821
    return n


@ipp.interactive
def func(x):
    return x * x


mixed = list(map(str, range(10)))
completed = list(map(str, range(0, 10, 2)))
failed = list(map(str, range(1, 10, 2)))


class TestDependency(ClusterTestCase):
    def setup_method(self):
        super().setup_method()
        self.user_ns = {'__builtins__': __builtins__}
        self.view = self.client.load_balanced_view()
        self.dview = self.client[-1]
        self.succeeded = set(map(str, range(0, 25, 2)))
        self.failed = set(map(str, range(1, 25, 2)))

    def assertMet(self, dep):
        assert dep.check(self.succeeded, self.failed), "Dependency should be met"

    def assertUnmet(self, dep):
        assert not dep.check(self.succeeded, self.failed), (
            "Dependency should not be met"
        )

    def assertUnreachable(self, dep):
        assert dep.unreachable(self.succeeded, self.failed), (
            "Dependency should be unreachable"
        )

    def assertReachable(self, dep):
        assert not dep.unreachable(self.succeeded, self.failed), (
            "Dependency should be reachable"
        )

    def cancan(self, f):
        """decorator to pass through canning into self.user_ns"""
        return uncan(can(f), self.user_ns)

    def test_require_imports(self):
        """test that @require imports names"""

        @self.cancan
        @ipp.require('base64')
        @interactive
        def encode(arg):
            return base64.b64encode(arg)  # noqa: F821

        # must pass through canning to properly connect namespaces
        assert encode(b'foo') == b'Zm9v'

    def test_success_only(self):
        dep = ipp.Dependency(mixed, success=True, failure=False)
        self.assertUnmet(dep)
        self.assertUnreachable(dep)
        dep.all = False
        self.assertMet(dep)
        self.assertReachable(dep)
        dep = ipp.Dependency(completed, success=True, failure=False)
        self.assertMet(dep)
        self.assertReachable(dep)
        dep.all = False
        self.assertMet(dep)
        self.assertReachable(dep)

    def test_failure_only(self):
        dep = ipp.Dependency(mixed, success=False, failure=True)
        self.assertUnmet(dep)
        self.assertUnreachable(dep)
        dep.all = False
        self.assertMet(dep)
        self.assertReachable(dep)
        dep = ipp.Dependency(completed, success=False, failure=True)
        self.assertUnmet(dep)
        self.assertUnreachable(dep)
        dep.all = False
        self.assertUnmet(dep)
        self.assertUnreachable(dep)

    def test_require_function(self):
        @ipp.interactive
        def bar(a):
            return func(a)

        @ipp.require(func)
        @ipp.interactive
        def bar2(a):
            return func(a)

        self.client[:].clear()
        with raises_remote(NameError):
            self.view.apply_sync(bar, 5)
        ar = self.view.apply_async(bar2, 5)
        assert ar.get(5) == func(5)

    def test_require_object(self):
        @ipp.require(foo=func)
        @ipp.interactive
        def bar(a):
            return foo(a)  # noqa: F821

        ar = self.view.apply_async(bar, 5)
        assert ar.get(5) == func(5)
