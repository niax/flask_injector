# encoding: utf-8
#
# Copyright (C) 2012 Alec Thomas <alec@swapoff.org>
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution.
#
# Author: Alec Thomas <alec@swapoff.org>

"""Flask-Injector - A dependency-injection adapter for Flask.

The following example illustrates function and class-based views with
dependency-injection:

    @route("/bar")
    def bar():
        return render("bar.html")


    # Route with injection
    @route("/foo")
    @inject(db=sqlite3.Connection)
    def foo(db):
        users = db.execute('SELECT * FROM users').all()
        return render("foo.html")


    @route('/waz')
    class Waz(object):
        @inject(db=sqlite3.Connection)
        def __init__(self, db):
            self.db = db

        @route("/waz")
        def waz(self):
            users = db.execute('SELECT * FROM users').all()
            return 'waz'


    def configure(binder):
        config = binder.injector.get(Config)
        binder.bind(
            sqlite3.Connection,
            to=sqlite3.Connection(config['DB_CONNECTION_STRING']),
            scope=request,
            )


    def main():
        views = [foo, bar, Waz]
        modules = [configure]
        app = Builder(views, modules, config={
            'DB_CONNECTION_STRING': ':memory:',
            }).build()
        app.run()
"""

from __future__ import absolute_import

import inspect
from werkzeug.local import Local, LocalManager
from injector import Injector, Scope, ScopeDecorator, singleton, InstanceProvider
import flask
from flask import Config, Request
from flask.views import View


__author__ = 'Alec Thomas <alec@swapoff.org>'
__version__ = '0.1.0'
__all__ = ['Builder', 'request', 'RequestScope', 'Config', 'Request', 'decorator', 'route']


class InjectorView(View):
    """A Flask View that applies argument injection to a decorated function."""

    def __init__(self, handler, injector, handler_class=None):
        self._handler = handler
        self._injector = injector
        self._handler_class = handler_class

    def dispatch_request(self, **kwargs):
        # Not @injected
        request_scope = self._injector.get(RequestScope)
        handler = self._handler
        if self._handler_class:
            instance = self._injector.get(self._handler_class)
            handler = self._handler.__get__(instance, self._handler_class)
        if not hasattr(handler, '__bindings__'):
            return handler(**kwargs)
        bindings = self._injector.args_to_inject(
            function=handler,
            bindings=handler.__bindings__,
            owner_key=handler.__module__,
            )
        try:
            return self._handler(**dict(bindings, **kwargs))
        finally:
            request_scope.reset()


class RequestScope(Scope):
    """A scope whose object lifetime is tied to a request.

    @request
    class Session(object):
        pass
    """

    def reset(self):
        self._local_manager.cleanup()
        self._locals.scope = {}

    def configure(self):
        self._locals = Local()
        self._local_manager = LocalManager([self._locals])
        self.reset()

    def get(self, key, provider):
        try:
            return self._locals.scope[key]
        except KeyError:
            provider = InstanceProvider(provider.get())
            self._locals.scope[key] = provider
            return provider


request = ScopeDecorator(RequestScope)


def route(*args, **kwargs):
    """Decorate a function as a view endpoint."""
    def _wrap(f):
        f.__view__ = (args, kwargs)
        return f
    return _wrap


class decorator(object):
    """Convert a Flask extension decorator to a Flask-Injector decorator.

    Normally, Flask extension decorators are used like so:

        app = Flask(__name__)
        cache = Cache(app)

        @cache.cached(timeout=30)
        def route():
            return 'Hello world'

    As this requires global state (Flask app and Cache object), this class
    exists to inject the provided class instance on-demand. eg.

        cached = decorator(Cache.cached)

        ...

        @cached(timeout=30)
        def route():
            return 'Hello world'

    The Cache instance must be provided in an Injector module:

        class CacheModule(Module):
            @provides(Cache)
            @singleton
            @inject(app=Flask)
            def provides_cache(self, app):
                return Cache(app)

        builder = Builder([view], [CacheModule()], config={
            # Cache configuration keys here
            })
        app = builder.build()
        app.run()
    """

    class State(object):
        def __init__(self, f):
            self.f = f
            self.args = None
            self.kwargs = None

        def apply(self, injector, view):
            cls = self.f.im_class
            instance = injector.get(cls)
            decorator = self.f.__get__(instance, cls)
            return decorator(*self.args, **self.kwargs)(view)

    # Mapping from extension type to extension decorator
    ext_registry = {}
    state_registry = []

    def __init__(self, f):
        # cached = decorator(Cache.cached)
        decorator.ext_registry[f.im_class] = f
        self.state = decorator.State(f)
        decorator.state_registry.append(self.state)

    def __call__(self, *args, **kwargs):
        # @cached(timeout=30)
        self.state.args = args
        self.state.kwargs = kwargs

        def wrap(f):
            if not hasattr(f, '__decorators__'):
                f.__decorators__ = []
            f.__decorators__.append(self.state)
            return f

        return wrap


class Builder(object):
    """Builds an Injector-enabled Flask app.

    Use it like so:

    >>> builder = Builder()
    >>> app = builder.build()  # "app" is a Flask instance

    The created Flask instance has an additional "injector" attribute.

    Objects bound to the Injector are by the builder are:
    - Support for per-request scopes via the @request decorator.
    - The Flask application object (flask.Flask).
    - The Flask configuration object (flask.Config).
    - The current Flask request (flask.Request, usually available as flask.request).

    """
    def __init__(self, views=None, modules=None, config=None, package='__main__'):
        """Create a new Builder.

        :param views: List of Injector-enabled views to add to the Flask app.
        :param modules: List of Injector Modules to use to configure DI.
        :param config: Flask configuration dictionary.
        :param package: Package name passed to Flask constructor.
        """
        self._views = views or []
        self._modules = modules or []
        self._config = config or {}
        self._package = package

    def build(self):
        """Build Flask app."""
        injector = Injector(self._configure)
        app = injector.get(flask.Flask)
        app.injector = injector
        return app

    def _configure(self, binder):
        injector = binder.injector
        binder.bind_scope(RequestScope)
        app = flask.Flask(self._package)
        app.config.update(self._config)
        binder.bind(flask.Flask, to=app, scope=singleton)
        binder.bind(Config, to=app.config, scope=singleton)
        binder.bind(Request, to=lambda: flask.request)
        for module in self._modules:
            binder.install(module)

        # Generate views
        for view in self._views:
            if inspect.isclass(view):
                self._reflect_views_from_class(view, injector, app)
            else:
                assert hasattr(view, '__view__')
                iview = InjectorView.as_view(view.__name__, handler=view, injector=injector)
                iview = self._install_route(injector, app, view, iview, *view.__view__)

    def _reflect_views_from_class(self, cls, injector, app):
        class_view = getattr(cls, '__view__', None)
        assert class_view is None or len(class_view[0]) == 1, \
            'Path prefix is the only non-keyword argument allowed on class @view for ' + str(cls)
        prefix = class_view[0][0] if class_view is not None else ''
        class_kwargs = class_view[1]
        for name, method in inspect.getmembers(cls, lambda m: inspect.ismethod(m) and hasattr(m, '__view__')):
            args, kwargs = method.__view__
            args = (prefix + args[0],) + args[1:]
            kwargs = dict(class_kwargs, **kwargs)
            iview = InjectorView.as_view(name, handler=method, injector=injector, handler_class=cls)
            self._install_route(injector, app, method, iview, args, kwargs)

    def _install_route(self, injector, app, view, iview, args, kwargs):
        if hasattr(view, '__decorators__'):
            for state in view.__decorators__:
                iview = state.apply(injector, iview)
        print args, kwargs
        app.add_url_rule(*args, view_func=iview, **kwargs)