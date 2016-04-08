import gc
import inspect
from asyncio import get_event_loop, Future
from concurrent.futures import Executor
from functools import wraps, partial
from threading import Event
from typing import Optional, Callable, Union

__all__ = ('threadpool', 'call_in_executor')


class _ThreadSwitcher:
    __slots__ = 'executor', 'exited', 'event_loop_thread'

    def __init__(self, executor: Optional[Executor]):
        self.executor = executor
        self.exited = False

    def __aenter__(self):
        # This is run in the event loop thread
        return self

    def __await__(self):
        def exec_when_ready():
            event.wait()
            coro.send(None)

            if not self.exited:
                raise RuntimeError('attempted to "await" in a worker thread')

        if self.exited:
            # This is run in the worker thread
            yield
        else:
            # This is run in the event loop thread
            previous_frame = inspect.currentframe().f_back
            coro = next(obj for obj in gc.get_referrers(previous_frame.f_code)
                        if inspect.iscoroutine(obj))
            event = Event()
            loop = get_event_loop()
            future = loop.run_in_executor(self.executor, exec_when_ready)
            next(future.__await__())  # Make the future think it's being awaited on
            loop.call_soon(event.set)
            yield future

    def __aexit__(self, exc_type, exc_val, exc_tb):
        # This is run in the worker thread
        self.exited = True
        return self

    def __call__(self, func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                loop = get_event_loop()
            except RuntimeError:
                # Event loop not available -- we're in a worker thread
                return func(*args, **kwargs)
            else:
                callback = partial(func, *args, **kwargs)
                return loop.run_in_executor(self.executor, callback)

        assert not inspect.iscoroutinefunction(func), \
            'Cannot wrap coroutine functions to be run in an executor'
        return wrapper


def threadpool(arg: Union[Executor, Callable] = None):
    """
    Return a decorator/asynchronous context manager that guarantees that the wrapped function or
    ``with`` block is run in the given executor.

    If no executor is given, the current event loop's default executor is used.
    Otherwise, the executor must be a PEP 3148 compliant thread pool executor.

    Callables wrapped with this must be used with ``await`` when called in the event loop thread.
    They can also be called in worker threads, just by omitting the ``await``.

    Example use as a decorator::

        @threadpool
        def this_runs_in_threadpool():
           return do_something_cpu_intensive()

        async def request_handler():
            result = await this_runs_in_threadpool()

    Example use as an asynchronous context manager::

        async def request_handler(in_url, out_url):
            page = await http_fetch(in_url)

            async with threadpool():
                data = transform_page(page)

            await http_post(out_url, page)

    :param arg: either a callable (when used as a decorator) or an executor in which to run the
        wrapped callable or the ``with`` block (when used as a context manager)

    """
    if callable(arg):
        # When used like @threadpool
        return _ThreadSwitcher(None)(arg)
    else:
        # When used like @threadpool(...) or async with threadpool(...)
        return _ThreadSwitcher(arg)


def call_in_executor(func: Callable, *args, executor: Executor = None, **kwargs) -> Future:
    """
    Call the given callable in an executor.

    This is a nicer version of the following::

        get_event_loop().run_in_executor(executor, func, *args)

    There is virtually never a need to use :func:`functools.partial` with this function.
    The only such case would be if you want to pass a keyword argument ``executor`` to the target
    callable.

    :param func: a function
    :param args: positional arguments to call with
    :param executor: the executor to call the function in
    :param kwargs: keyword arguments to call with
    :return: a future that will resolve to the function call's return value

    """
    callback = partial(func, *args, **kwargs)
    return get_event_loop().run_in_executor(executor, callback)
