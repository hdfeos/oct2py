import types
import warnings
import weakref

import numpy as np
from scipy.io.matlab.mio5 import MatlabObject

from oct2py.compat import PY2
from oct2py.utils import get_nout


class OctavePtr(object):
    """A pointer to an Octave workspace value.
    """

    def __init__(self, session_weakref):
        self._ref = session_weakref
        self.__module__ = 'oct2py.dynamic'
        self._name = name

    @property
    def address(self):
        return self._name


class _DocDescriptor(object):
    """An object that dynamically fetches the documentation
    for an Octave value.
    """

    def __init__(self, session_weakref, name):
        self.ref = session_weakref
        self.name = name
        self.doc = None

    def __get__(self, instance, owner=None):
        if self.doc:
            return self.doc
        self.doc = self.ref()._get_doc(self.name)
        return self.doc


class OctaveVariablePtr(OctavePtr):
    """An object that acts as a pointer to an Octave value.
    """

    @property
    def value(self):
        return self._ref().pull(self._name)

    @value.setter
    def value(self, obj):
        self._ref().push(self._name, obj)


class OctaveFunctionPtr(OctavePtr):
    """An object that acts as a pointer to an Octave function.
    """

    def __init__(self, session_weakref, name):
        OctavePtr.__init__(self, session_weakref, name)
        self.__name__ = name

    @property
    def address(self):
        return '@%s' % self._name

    def __call__(self, *inputs, **kwargs):
        # Check for allowed keyword arguments
        nout = kwargs.pop('nout', get_nout())
        allowed = ['verbose', 'store_as', 'timeout', 'stream_handler',
                   'plot_dir', 'plot_name', 'plot_format', 'plot_width',
                   'plot_height', 'plot_res']

        extras = {}
        for (key, value) in kwargs.items():
            if key not in allowed:
                extras[key] = kwargs.pop(key)

        if extras:
            warnings.warn('Key - value pairs are deprecated, use `func_args`')

        inputs += tuple(item for pair in zip(extras.keys(), extras.values())
                        for item in pair)

        return self._ref().feval(self._name, *inputs,
            nout=nout, **kwargs)

    def __repr__(self):
        return '"%s" Octave function' % self._name


class OctaveUserClassAttr(OctavePtr):
    """An attribute associated with an Octave user class instance.
    """

    def __get__(self, instance, owner=None):
        if instance is None:
            return 'dynamic attribute'
        return instance._ref().feval('get', instance, self._name)

    def __set__(self, instance, value):
        if instance is None:
            return
        # The set function returns a new struct, so we have to re-set it.
        instance._ref().feval('set', instance, self._name, value,
                              store_as=instance._address)


class _MethodDocDescriptor(object):
    """An object that dynamically fetches the documentation
    for an Octave user class method.
    """

    def __init__(self, session_weakref, class_name, name):
        self.ref = session_weakref
        self.class_name = class_name
        self.name = name
        self.doc = None

    def __get__(self, instance, owner=None):
        if self.doc is not None:
            return self.doc
        session = self.ref()
        class_name = self.class_name
        method = self.name
        doc = session._get_doc('@%s/%s' % (class_name, method))
        self.doc = doc or session._get_doc(method)
        return self.doc


class OctaveUserClassMethod(OctaveFunctionPtr):
    """A method for a user defined Octave class.
    """

    def __init__(self, session_weakref, name, class_name):
        OctaveFunctionPtr.__init__(self, session_weakref, name)
        self._class_name = class_name

    def __get__(self, instance, owner=None):
        # Bind to the instance.
        if PY2:
            return types.MethodType(self, instance, owner)
        return types.MethodType(self, instance)

    def __call__(self, instance, *inputs, **kwargs):
        nout = kwargs.get('nout', get_nout())
        inputs = [instance] + list(inputs)
        self._ref().feval(self._name, *inputs, nout=nout, **kwargs)

    def __repr__(self):
        return '"%s" Octave method for object' % (self._name,
                                                  self._class_name)


class OctaveUserClass(object):
    """A wrapper for an Octave user class.
    """

    def __init__(self, *inputs, **kwargs):
        """Create a new instance with the user class constructor."""
        addr = self._address = '%s_%s' % (self._name, id(self))
        self._ref().feval(self._name, *inputs, store_as=addr, **kwargs)

    @classmethod
    def from_value(cls, value):
        """This is how an instance is created when we read a
           MatlabObject from a MAT file.
        """
        self = OctaveUserClass.__new__(cls)
        self._address = '%s_%s' % (self._name, id(self))
        self._ref().push(self._address, value)
        return self

    @classmethod
    def to_value(cls, instance):
        """Convert to a value to send to Octave."""
        if not isinstance(instance, OctaveUserClass) or not instance._attrs:
            return dict()
        # Bootstrap a MatlabObject from scipy.io
        # From https://github.com/scipy/scipy/blob/93a0ea9e5d4aba1f661b6bb0e18f9c2d1fce436a/scipy/io/matlab/mio5.py#L435-L443
        # and https://github.com/scipy/scipy/blob/93a0ea9e5d4aba1f661b6bb0e18f9c2d1fce436a/scipy/io/matlab/mio5_params.py#L224
        dtype = []
        values = []
        for attr in instance._attrs:
            dtype.append((attr, object))
            values.append(getattr(instance, attr))
        struct = np.array([tuple(values)], dtype)
        return MatlabObject(struct, instance._class_name)


def _make_user_class(session, name):
    """Make an Octave class for a given class name"""
    attrs = session.eval('ans = fieldnames(%s);' % name)
    methods = session.eval('ans = methods(%s);' % name)
    ref = weakref.ref(session)

    doc = _DocDescriptor(ref, name)
    values = dict(__doc__=doc, _name=name, _ref=ref, _attrs=attrs,
                  __module__='oct2py.dynamic')

    for method in methods:
        doc = _MethodDocDescriptor(ref, name, method)
        cls_name = '%s_%s' % (name, method)
        method_values = dict(__doc__=doc)
        method_cls = type(cls_name, (OctaveUserClassMethod,), method_values)
        values[method] = method_cls(ref, method, name)

    for attr in attrs:
        values[attr] = OctaveUserClassAttr(ref, attr)

    return type(name, (OctaveUserClass,), values)


def _make_function_ptr_instance(session, name):
    ref = weakref.ref(session)
    doc = _DocDescriptor(ref, name)
    custom = type(name, (OctaveFunctionPtr,), dict(__doc__=doc))
    return custom(ref, name)


def _make_variable_ptr_instance(session, name):
    """Make a pointer instance for a given variable by name.
    """
    doc = '%s is a variable' % name
    custom = type(name, (OctavePtr,), dict(__doc__=doc))
    return custom(weakref.ref(session), name)
