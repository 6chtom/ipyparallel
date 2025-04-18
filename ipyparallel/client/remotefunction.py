"""Remote Functions and decorators for Views."""

# Copyright (c) IPython Development Team.
# Distributed under the terms of the Modified BSD License.
import warnings
from inspect import signature

from decorator import decorator

from ..serialize import PrePickled
from . import map as Map
from .asyncresult import AsyncMapResult

# -----------------------------------------------------------------------------
# Functions and Decorators
# -----------------------------------------------------------------------------


def remote(view, block=None, **flags):
    """Turn a function into a remote function.

    This method can be used for map::

        In [1]: @remote(view,block=True)
           ...: def func(a):
           ...:    pass
    """

    def remote_function(f):
        return RemoteFunction(view, f, block=block, **flags)

    return remote_function


def parallel(view, dist='b', block=None, ordered=True, **flags):
    """Turn a function into a parallel remote function.

    This method can be used for map::

        In [1]: @parallel(view, block=True)
           ...: def func(a):
           ...:    pass
    """

    def parallel_function(f):
        return ParallelFunction(
            view, f, dist=dist, block=block, ordered=ordered, **flags
        )

    return parallel_function


def getname(f):
    """Get the name of an object.

    For use in case of callables that are not functions, and
    thus may not have __name__ defined.

    Order: f.__name__ >  f.name > str(f)
    """
    try:
        return f.__name__
    except Exception:
        pass
    try:
        return f.name
    except Exception:
        pass

    return str(f)


@decorator
def sync_view_results(f, self, *args, **kwargs):
    """sync relevant results from self.client to our results attribute.

    This is a clone of view.sync_results, but for remote functions
    """
    view = self.view
    if view._in_sync_results:
        return f(self, *args, **kwargs)
    view._in_sync_results = True
    try:
        ret = f(self, *args, **kwargs)
    finally:
        view._in_sync_results = False
        view._sync_results()
    return ret


# --------------------------------------------------------------------------
# Classes
# --------------------------------------------------------------------------


class RemoteFunction:
    """Turn an existing function into a remote function.

    Parameters
    ----------

    view : View instance
        The view to be used for execution
    f : callable
        The function to be wrapped into a remote function
    block : bool [default: None]
        Whether to wait for results or not.  The default behavior is
        to use the current `block` attribute of `view`

    **flags : remaining kwargs are passed to View.temp_flags
    """

    view = None  # the remote connection
    func = None  # the wrapped function
    block = None  # whether to block
    flags = None  # dict of extra kwargs for temp_flags

    def __init__(self, view, f, block=None, **flags):
        self.view = view
        self.func = f
        self.block = block
        self.flags = flags

        # copy function attributes for nicer inspection
        # of decorated functions
        self.__name__ = getname(f)
        if getattr(f, '__doc__', None):
            self.__doc__ = f'{self.__class__.__name__} wrapping:\n{f.__doc__}'
        if getattr(f, '__signature__', None):
            self.__signature__ = f.__signature__
        else:
            try:
                self.__signature__ = signature(f)
            except Exception:
                # no signature, but that's okay
                pass

    def __call__(self, *args, **kwargs):
        block = self.view.block if self.block is None else self.block
        with self.view.temp_flags(block=block, **self.flags):
            return self.view.apply(self.func, *args, **kwargs)


def _map(f, *sequences):
    return list(map(f, *sequences))


_prepickled_map = None


class ParallelFunction(RemoteFunction):
    """Class for mapping a function to sequences.

    This will distribute the sequences according the a mapper, and call
    the function on each sub-sequence.  If called via map, then the function
    will be called once on each element, rather that each sub-sequence.

    Parameters
    ----------

    view : View instance
        The view to be used for execution
    f : callable
        The function to be wrapped into a remote function
    dist : str [default: 'b']
        The key for which mapObject to use to distribute sequences
        options are:

        * 'b' : use contiguous chunks in order
        * 'r' : use round-robin striping

    block : bool [default: None]
        Whether to wait for results or not.  The default behavior is
        to use the current `block` attribute of `view`
    chunksize : int or None
        The size of chunk to use when breaking up sequences in a load-balanced manner
    ordered : bool [default: True]
        Whether the result should be kept in order. If False,
        results become available as they arrive, regardless of submission order.
    return_exceptions : bool [default: False]
    **flags
        remaining kwargs are passed to View.temp_flags
    """

    chunksize = None
    ordered = None
    mapObject = None

    def __init__(
        self,
        view,
        f,
        dist='b',
        block=None,
        chunksize=None,
        ordered=True,
        return_exceptions=False,
        **flags,
    ):
        super().__init__(view, f, block=block, **flags)
        self.chunksize = chunksize
        self.ordered = ordered
        self.return_exceptions = return_exceptions

        mapClass = Map.dists[dist]
        self.mapObject = mapClass()

    @sync_view_results
    def __call__(self, *sequences, **kwargs):
        global _prepickled_map
        if _prepickled_map is None:
            _prepickled_map = PrePickled(_map)
        client = self.view.client
        _mapping = kwargs.pop('__ipp_mapping', False)
        if kwargs:
            raise TypeError(f"Unexpected keyword arguments: {kwargs}")

        lens = []
        maxlen = minlen = -1
        for i, seq in enumerate(sequences):
            try:
                n = len(seq)
            except Exception:
                seq = list(seq)
                if isinstance(sequences, tuple):
                    # can't alter a tuple
                    sequences = list(sequences)
                sequences[i] = seq
                n = len(seq)
            if n > maxlen:
                maxlen = n
            if minlen == -1 or n < minlen:
                minlen = n
            lens.append(n)

        if maxlen == 0:
            # nothing to iterate over
            return []

        # check that the length of sequences match
        if not _mapping and minlen != maxlen:
            msg = f'all sequences must have equal length, but have {lens}'
            raise ValueError(msg)

        balanced = 'Balanced' in self.view.__class__.__name__
        if balanced:
            if self.chunksize:
                nparts = maxlen // self.chunksize + int(maxlen % self.chunksize > 0)
            else:
                nparts = maxlen
            targets = [None] * nparts
        else:
            if self.chunksize:
                warnings.warn(
                    "`chunksize` is ignored unless load balancing", UserWarning
                )
            # multiplexed:
            targets = self.view.targets
            # 'all' is lazily evaluated at execution time, which is now:
            if targets == 'all':
                targets = client._build_targets(targets)[1]
            elif isinstance(targets, int):
                # single-engine view, targets must be iterable
                targets = [targets]
            nparts = len(targets)

        futures = []

        pf = PrePickled(self.func)

        chunk_sizes = {}
        chunk_size = 1

        for index, t in enumerate(targets):
            args = []
            for seq in sequences:
                part = self.mapObject.getPartition(seq, index, nparts, maxlen)
                args.append(part)

            if sum(len(arg) for arg in args) == 0:
                continue

            if _mapping:
                chunk_size = min(len(arg) for arg in args)

            args = [PrePickled(arg) for arg in args]

            if _mapping:
                f = _prepickled_map
                args = [pf] + args
            else:
                f = pf

            view = self.view if balanced else client[t]
            with view.temp_flags(block=False, **self.flags):
                ar = view.apply(f, *args)
                ar.owner = False

            msg_id = ar.msg_ids[0]
            chunk_sizes[msg_id] = chunk_size
            futures.extend(ar._children)

        r = AsyncMapResult(
            self.view.client,
            futures,
            self.mapObject,
            fname=getname(self.func),
            ordered=self.ordered,
            return_exceptions=self.return_exceptions,
            chunk_sizes=chunk_sizes,
        )

        if self.block:
            try:
                return r.get()
            except KeyboardInterrupt:
                return r
        else:
            return r

    def map(self, *sequences):
        """call a function on each element of one or more sequence(s) remotely.
        This should behave very much like the builtin map, but return an AsyncMapResult
        if self.block is False.

        That means it can take generators (will be cast to lists locally),
        and mismatched sequence lengths will be padded with None.
        """
        return self(*sequences, __ipp_mapping=True)


__all__ = ['remote', 'parallel', 'RemoteFunction', 'ParallelFunction']
